"""Parallel execution helpers.

`run_parallel` is the single entry point every parallel pass in Isocenter
goes through -- scanning, exporting, verification. It picks between three
execution strategies and adapts to a set of `ISOCENTER_*` environment
variables, so that tuning a cohort run never means editing code.
"""
import collections
import concurrent.futures
import functools
import itertools
import os
import signal
import sys
import multiprocessing
import multiprocessing.connection
import threading
import time
import weakref
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
#: before the kernel ends it with SIGALRM (#844). No environment variable:
#: tests patch it. Resolved in the parent and handed to the worker as an
#: argument, so a patched value reaches a spawned child.
#:
#: Armed only under `ISOCENTER_WORKER_FAULTHANDLER=1`, by the `atexit` that
#: `_worker_init` registers. The recycling pool's workers armed it at their
#: quota too until #885 (#888); the parent now ends one that cannot leave.
#: Below conftest's `_STALL_S`, pytest's faulthandler window and
#: `_WORKER_FAULTHANDLER_TIMEOUT_S`. `test_packaging_contract.py` pins it.
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
    # tests. `new_spawn_process_pool` calls it for every process pool in
    # the package (#667), so every pool agrees on what a worker runs first.
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


#: The time bound on one task of `ingest()` and `export()` when
#: `ISOCENTER_TASK_TIMEOUT` is unset (owner ruling Q5 on #673, 2026-11-21).
#: A legitimate multi-gigabyte multi-frame file on a slow disk was the
#: scenario it was weighed against; the variable lifts it.
_DEFAULT_TASK_TIMEOUT_S = 600


def resolve_task_timeout() -> Optional[float]:
    """The time bound on one task, in seconds, or None for no bound (#673).

    `ISOCENTER_TASK_TIMEOUT`, a whole number of seconds; `0` means no
    bound. Unset, negative or not a whole number: 600 (the last two with a
    WARNING naming the variable).

    Returns:
        Optional[float]: The bound, or None.
    """
    # `minimum=0`, so `0` reaches here and is the "no bound" spelling; a
    # bound of 0 s would stop every task at once. Read at each call's
    # dispatch, not at import, so a value set after import applies, and once
    # per call, so every round of one `ingest()` and the row it writes name
    # the same bound.
    configured = _env_int("ISOCENTER_TASK_TIMEOUT", minimum=0)
    if configured is None:
        return _DEFAULT_TASK_TIMEOUT_S
    return configured or None


class _TaskDidNotFinish(Exception):
    """A task that ran past the time bound twice, the second time alone on a
    fresh worker process, which was then stopped (#673). A task of a chunk
    of several is charged only with its runs alone, so it was stopped three
    times: once in its chunk, then twice alone.

    Made in the parent, never pickled. Yielded in the task's place under
    `yield_exceptions`, raised otherwise.

    Attributes:
        item: The item whose task did not finish.
        bound (float): The bound, in seconds.
    """

    def __init__(self, item, bound):
        super().__init__(
            f"the task did not finish within the {bound:g} s bound "
            f"(ISOCENTER_TASK_TIMEOUT) twice, the second time alone on a "
            f"fresh worker process; each worker it ran on was stopped")
        self.item = item
        self.bound = bound


class _RanPastTheBound(BrokenProcessPool):
    """A shared process pool whose workers were sent SIGKILL because the
    caller waited on one task for longer than the bound (#673).

    A `BrokenProcessPool`, because that is what the pool now is, and every
    caller that survives a dead worker (`_ingest_results`) survives this.
    It accuses no task: a pool runs several at once, and the one waited on
    is only the likeliest.
    """

    def __init__(self, bound, waited):
        super().__init__(
            f"no result came back for {waited:.1f} s, past the {bound:g} s "
            f"bound (ISOCENTER_TASK_TIMEOUT), so every worker process of "
            f"the pool was sent SIGKILL")
        self.bound = bound


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
    # Recycling beats threads because recycling replaces a worker process,
    # which a thread is not (`_SlotPool`, #885). The whole precedence
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
        "was also asked for, and recycling replaces worker "
        "processes, so this run uses processes. "
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
#: its state on SIGTERM must get to finish: coverage's `sigterm = True`
#: handler, which this repository's `.coveragerc` set until #886, took up
#: to 0.8 s under load to write the worker's data file.
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
    task's failure; coverage's `sigterm = True` (this repository's
    `.coveragerc` set it until #886).

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
        killed = _kill_still_running(waiting.values())
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


class _HeadWatch:
    """Ends a shared process pool's workers when its reader has waited on
    one result for longer than the bound (#673).

    `executor.map` hands out no future and no start time, so a task's own
    running time cannot be read on a pool the caller shares. What can be
    read is how long the reader has been blocked on the next result. On an
    ordered `map` that result is the oldest task not returned, and every
    task before it has returned, so it was the next one taken; the wait
    can only be longer than its running time, never shorter. A stop is
    therefore never early for the task waited on, and may be later.

    It is a suspicion, not a verdict: the pool runs other tasks beside it,
    and a peer `ingest()` sharing the pool may have queued its own tasks
    ahead. The workers are sent SIGKILL, the reader's next result raises
    `_RanPastTheBound`, and the caller's retry reads each file alone, where
    the same watch is exact (owner ruling Q9).
    """

    def __init__(self, executor, bound):
        self._executor = executor
        self._bound = bound
        # A float or None, written by the reader and read by the watch
        # thread: one reference store, atomic on every build.
        self._since = None
        self._stop = threading.Event()
        self.waited = None   # seconds waited when the watch fired
        self._thread = threading.Thread(
            target=self._watch, name="isocenter-task-bound", daemon=True)

    def start(self):
        self._thread.start()

    def close(self):
        """Stop the watch thread; wait for it, bounded. Safe to call twice."""
        self._stop.set()
        if self._thread.ident is not None:
            # Bounded: the thread wakes on the event at once, and at worst
            # is mid-kill, which takes no lock the reader holds.
            self._thread.join(5.0)

    def wrap(self, iterator):
        """Yield from `iterator`, timing each wait on the next result."""
        while True:
            self._since = time.monotonic()
            try:
                result = next(iterator)
            except StopIteration:
                return
            except BrokenProcessPool as exc:
                if self.waited is not None:
                    raise _RanPastTheBound(self._bound, self.waited) from exc
                raise
            finally:
                self._since = None
            yield result

    def _watch(self):
        while not self._stop.is_set():
            since = self._since
            if since is not None:
                waited = time.monotonic() - since
                if waited >= self._bound:
                    self._fire(waited)
                    return
                wait = min(self._bound - waited, _POOL_WATCH_S)
            else:
                wait = _POOL_WATCH_S
            self._stop.wait(wait)

    def _fire(self, waited):
        # The workers of the executor handed in, read now: never a pool the
        # session swapped in since, and never one idle between calls,
        # because the watch runs only while its reader waits.
        values = _workers_of(self._executor)
        self.waited = waited
        killed = _kill_each(values)
        get_logger().warning(
            "No result came back from a shared process pool for %.1f s, "
            "past the %g s bound (ISOCENTER_TASK_TIMEOUT); its %d worker "
            "process(es) were sent SIGKILL (pid %s). The tasks they held are "
            "read again, each alone, before any is reported (#673).",
            waited, self._bound, len(killed),
            ", ".join(str(pid) for pid in killed))


def _bounds_processes(executor) -> bool:
    """Whether `executor` is a process pool whose workers a bound can end:
    a `ProcessPoolExecutor`. A thread cannot be ended, and a caller's
    `multiprocessing.Pool` is not ours to kill."""
    return isinstance(executor, concurrent.futures.ProcessPoolExecutor)


def _run_on_shared_executor(executor, func, items, strategy,
                            task_timeout=None):
    """Uses an executor the caller owns, and does not shut it down.

    Args:
        executor: A `concurrent.futures` executor, or a caller's
            `multiprocessing.Pool` (whose `imap` is used).
        func: The worker function.
        items: The items to process.
        strategy (_Strategy): Its `chunksize` and progress settings apply.
        task_timeout (Optional[float]): With a `ProcessPoolExecutor`, the
            seconds the reader may wait on one result before every worker
            of the pool is sent SIGKILL and the stream raises
            `_RanPastTheBound`. None: no bound.

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
    watch = None
    if task_timeout is not None and _bounds_processes(executor):
        watch = _HeadWatch(executor, task_timeout)
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
        if watch is not None:
            watch.start()
            iterator = watch.wrap(iterator)
        yield from _tracked(iterator, items, strategy)
    finally:
        # The watch first: it must not fire on a pool the owner is about to
        # retire or hand to the next call.
        if watch is not None:
            watch.close()
        # On every way out, before the owner hears of it: the owner shuts
        # the pool down or retires it, and `shutdown()` takes the lock the
        # manager thread holds while it joins the workers. A future's
        # `BrokenProcessPool` is one way out of a broken pool; a reader
        # that stops (`GeneratorExit`), Ctrl-C and a task's own exception
        # are others, and `ingest()` shuts its own retry pools down after
        # any of them. On a pool that is not broken this costs a `getattr`.
        _end_broken_pool_stragglers(executor)


def _still_running(processes) -> list:
    """The processes of `processes` whose sentinel is not ready.

    The sentinels, never `join()` or `is_alive()`: those reap the child
    with `waitpid`, racing an executor's manager thread, which joins the
    same process (#861's rule). A process object already closed has
    nothing to end.
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


def _kill_each(processes) -> list[int]:
    """SIGKILL each process; return the pids the signal was sent to."""
    killed = []
    for process in processes:
        try:
            process.kill()
        except (OSError, ValueError, AttributeError):
            # AttributeError: a process object not yet started has no
            # `_popen` to signal, and nothing to end.
            continue
        killed.append(process.pid)
    return killed


def _kill_still_running(processes) -> list[int]:
    """SIGKILL the processes still running; return their pids.

    A Ctrl-C (or anything else) raised while the sentinels are asked is
    not swallowed: every process is killed, then it is raised. Skipping
    the kill there let the caller's next step wait on a running worker for
    good (review of #913); killing one that already ended costs nothing,
    because `kill()` skips a reaped process and swallows a gone one.
    """
    processes = list(processes)
    try:
        targets = _still_running_or_all(processes)
    except BaseException:
        _kill_each(processes)
        raise
    return _kill_each(targets)


def _wait_for_sentinels(processes, deadline) -> None:
    """Wait until every one of `processes` has ended, or until `deadline`
    (a `time.monotonic()` value). Raises nothing of its own: a handle that
    cannot be asked ends the wait, and the caller's kill decides."""
    waiting = {}
    for process in processes:
        try:
            waiting[process.sentinel] = process
        except ValueError:
            continue
    while waiting:
        left = deadline - time.monotonic()
        if left <= 0:
            return
        try:
            ready = multiprocessing.connection.wait(list(waiting),
                                                    timeout=left)
        except (OSError, ValueError):
            return
        for sentinel in ready:
            del waiting[sentinel]


def _workers_of(executor) -> list:
    """The worker processes of a `ProcessPoolExecutor`, read from its
    private `_processes`, which the manager thread can change while it is
    read; `[]` when there are none or it cannot be read."""
    processes = getattr(executor, "_processes", None) or {}
    for _ in range(3):
        try:
            return list(processes.values())
        except RuntimeError:  # the dict changed while it was read
            continue
    return []


def stop_process_pool(executor) -> list[int]:
    """End a spawned process pool on a way out before its work is done.

    For a pool the caller built and is leaving with work in flight: Ctrl-C,
    a reader that stops, a worker that does not answer. Its exit,
    `shutdown(wait=True)`, waits for good on a worker that never returns
    (#673). Instead each worker is sent SIGTERM, one still running
    `_BROKEN_POOL_GRACE_S` later is sent SIGKILL and named in one WARNING,
    and then the pool is shut down without waiting. A Ctrl-C during the
    wait sends the SIGKILL at once and goes on.

    Args:
        executor (concurrent.futures.ProcessPoolExecutor): The pool.

    Returns:
        list[int]: The pids sent SIGKILL, or `[]`.
    """
    processes = _workers_of(executor)
    started = time.monotonic()
    killed = []
    try:
        try:
            for process in processes:
                try:
                    process.terminate()
                except (OSError, ValueError, AttributeError):
                    continue
            _wait_for_sentinels(processes, started + _BROKEN_POOL_GRACE_S)
        finally:
            # A `finally`, never an `except`, and no `return` in it: a
            # second Ctrl-C must reach the caller, and must not skip the
            # kill (#861, round 2; review of #913).
            killed = _kill_still_running(processes)
            if killed:
                # Not "after the pool was told to stop" nor "after the pool
                # was found broken": tests count the other kill lines by
                # those words.
                get_logger().warning(
                    "%d worker process(es) were still running %.1f s after "
                    "they were sent SIGTERM as their pool was left with work "
                    "in flight, and were sent SIGKILL (pid %s).",
                    len(killed), time.monotonic() - started,
                    ", ".join(str(pid) for pid in killed))
    finally:
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except Exception:  # pylint: disable=broad-exception-caught
            pass
    return killed


#: How often the recycling pool's stream wakes while it waits for a result,
#: to look at workers that hold no task and at workers it has told to leave
#: (#887, #885). It is the latency of seeing an idle worker die; a busy
#: worker's death is seen at once, through its task's future. No
#: environment variable: tests patch it.
_POOL_WATCH_S = 1.0

#: How many chunks one worker of the recycling pool holds at a time (#885).
#: Two, measured: at one, `export()` ran 7-8% slower than
#: `multiprocessing.Pool`, because a worker sat idle for the round trip
#: between returning a result and being handed the next task. Do not raise
#: it without measuring: a worker that dies loses every chunk it holds,
#: and the isolation of #668 must run each of them again.
_SLOT_DEPTH = 2


def new_spawn_process_pool(max_workers, *, disable_gc=False):
    """The one place a spawned `ProcessPoolExecutor` is built (#667).

    The session's shared pool and its rebuilds, `ingest()`'s retry pools,
    `run_parallel`'s own process pool and every worker of the recycling
    pool come from here, so all of them start their workers the same way
    and run the same initializer.

    Args:
        max_workers (int): The pool's width.
        disable_gc (bool): Disable the garbage collector in each worker
            (`ISOCENTER_DISABLE_GC=1` does too).

    Returns:
        concurrent.futures.ProcessPoolExecutor: The pool, with no worker
        started yet.
    """
    # Spawn, not fork (#220, #250): a forked worker inherits the parent's
    # open SQLite handles and its sidecar file position. The platform
    # default is fork on Linux 3.12, where a forked worker's
    # `persist_pixel_data` stalled on `database is locked`; macOS spawns by
    # default, so a local run does not show the difference, and
    # `test_parallel_contract.py` pins the argument.
    #
    # `concurrent.futures.ProcessPoolExecutor` is read by attribute at call
    # time, and `resolve_worker_initializer` through this module's global:
    # tests replace both, and a patch of `parallel.resolve_worker_initializer`
    # reaches every process pool in the package through here.
    #
    # Never pass `max_tasks_per_child`: it deadlocks `map` on 3.12, the
    # floor, the first time a worker is replaced (measured). Recycling is
    # `_SlotPool`'s, by replacing whole one-worker pools.
    return concurrent.futures.ProcessPoolExecutor(
        max_workers=max_workers,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=resolve_worker_initializer(disable_gc))


def _run_chunk(func, chunk) -> list:
    """Run `func` on each item of `chunk`, in a worker of the recycling pool.

    Module scope: it pickles into the spawned child by reference.
    """
    return [func(item) for item in chunk]


class _Slot:  # pylint: disable=too-few-public-methods
    """One one-worker executor of `_SlotPool`, and what it holds."""

    __slots__ = ("executor", "process", "given", "inflight", "shut",
                 "submitted", "last_done", "alone")

    def __init__(self, executor, alone=False):
        self.executor = executor
        # The worker's process, captured once the first `submit()` has
        # spawned it: `shutdown()` sets the executor's `_processes` to None,
        # so a handle read later is no handle at all.
        self.process = None
        self.given = 0           # chunks handed to this worker, ever
        self.inflight = {}       # future -> (sequence, first index, chunk)
        self.shut = False        # `shutdown()` already called
        # For the time bound (#673): when each future was handed over, and
        # when this worker last returned one. One worker runs its chunks in
        # the order given, so the chunk at the head of `inflight` began at
        # the later of the two.
        self.submitted = {}      # future -> time.monotonic() at submit
        self.last_done = 0.0
        # A worker opened to run one task again alone (owner ruling Q8):
        # given nothing else, and not counted in the width.
        self.alone = alone

    def returned(self, _future):
        """A future's done callback; runs on the executor's manager thread.
        One float store, atomic on every build."""
        self.last_done = time.monotonic()


class _SlotPool:
    """The recycling pool: one-worker spawned executors, each replaced after
    `recycle_after` chunks (#885).

    At most `width` workers run tasks at once, each holding at most
    `depth` chunks. A worker that has been handed `recycle_after` chunks
    takes no more; when its last one returns it is told to leave, and the
    next chunk opens a new worker. A worker told to leave that is still
    running `_BROKEN_POOL_GRACE_S` later is sent SIGKILL and named in one
    WARNING. A worker that dies while the pool runs fails the stream with
    `BrokenProcessPool`, after the results already delivered, and the
    exception's `lost` attribute names the chunks it held.

    Args:
        width (int): How many workers run at once.
        recycle_after (Optional[int]): Chunks per worker; None never
            replaces one.
        disable_gc (bool): Disable the collector in each worker.
        depth (int): Chunks one worker holds at a time.
        task_timeout (Optional[float]): Seconds one chunk may run (#673).
            A chunk past it has its worker sent SIGKILL; the other chunk
            that worker held is run again, and each item of the stopped
            chunk is run once more, alone, on a fresh worker (at most
            `width` such workers at once). An item past the bound a second
            time comes back as `_TaskDidNotFinish`: as a value under
            `yield_exceptions`, raised otherwise. Only an item that was a
            chunk of its own is charged with its chunk's stop; an item of
            a chunk of several is charged with its runs alone only. None:
            no bound.

    Raises:
        ValueError: `width` or `depth` below 1, or `recycle_after` neither
            None nor a positive int.
    """
    # Why one-worker executors, each its own pool, and not something
    # tidier. Each of these is a trap someone would "simplify" back:
    #
    # 1. Not `multiprocessing.Pool`. A worker killed while it waited for
    #    work left the inqueue's read lock held for good, and the pool's
    #    exit then waited on it (#885: 19 s and a thread left behind).
    # 2. Not one W-wide executor replaced every W*N tasks, as #885 first
    #    proposed. It ends only when its slowest task ends, so every other
    #    worker idles behind it (+66% measured with 1% of tasks at 2 s). And
    #    a W-wide executor that loses a worker SIGTERMs and joins the others
    #    while it holds the lock the next `submit()` takes (the #796/#861
    #    shape). A one-worker executor has no sibling to join, so that
    #    shape cannot occur in it: the one worker is what removes it, not a
    #    different spelling of the same pool.
    # 3. Not `ProcessPoolExecutor(max_tasks_per_child=)`: it deadlocks
    #    `map` on 3.12 at the first replacement (measured).
    # 4. Never `shutdown(wait=True)` on the caller's thread: it waits for
    #    good on a worker that cannot leave. Every worker is ended here,
    #    within a bound, through the handle captured when it was spawned.
    # 5. Sentinels, never `join()` or `is_alive()` on a handle: the
    #    executor's manager thread joins the same process.
    # 6. A death is a future's `BrokenProcessPool`, never a nonzero exit
    #    code: a worker whose initializer raises exits with code 0.

    def __init__(self, width, *, recycle_after, disable_gc,
                 depth=_SLOT_DEPTH, task_timeout=None):
        if not isinstance(width, int) or width < 1:
            raise ValueError(f"max_workers must be at least 1, not {width!r}")
        if recycle_after is not None and (
                not isinstance(recycle_after, int) or recycle_after < 1):
            raise ValueError(
                f"maxtasksperchild must be a positive int or None, not "
                f"{recycle_after!r}")
        if not isinstance(depth, int) or depth < 1:
            raise ValueError(f"depth must be at least 1, not {depth!r}")
        self._width = width
        self._recycle_after = recycle_after
        self._disable_gc = disable_gc
        self._depth = depth
        self._live = []       # slots not yet told to leave
        self._retiring = []   # (process, told to leave at): told to leave
        self._chunks = None
        self._func = None
        self._bound = task_timeout
        # The time bound's queues (#673). `_again`: chunks a stopped worker
        # held behind the one that ran too long, which never started and
        # are run again as they were. `_confirm`: one item each, of a chunk
        # that ran too long, to run alone. `_pieces`: those items' results,
        # gathered back into their chunk's sequence.
        self._again = collections.deque()
        self._confirm = collections.deque()
        self._pieces = {}
        # The stops each item run alone has been charged with, keyed by its
        # piece sequence. A chunk of one item that was stopped charges that
        # item 1: it was running. A chunk of several charges none of them:
        # which one was running cannot be read in the parent, and the items
        # behind it never started, so a stop alone is then an item's first
        # (owner ruling Q8; found in the review of #673). Only a stop that
        # brings an item to 2 rejects it.
        self._strikes = {}
        self._yields_values = False

    def _started(self, process):
        """Called with each worker's process once it is spawned. A hook for
        tests, which record every worker to end it on their way out."""

    # --- feeding ----------------------------------------------------------

    def _next_chunk(self):
        """The next `(sequence, first index, chunk)`, or None when the items
        are used up. Read one chunk at a time: `items` may be an iterator,
        and `export_batch` hands one."""
        if self._chunks is None:
            return None
        chunk = next(self._chunks, None)
        if chunk is None:
            self._chunks = None
        return chunk

    def _accepting(self, slot) -> bool:
        if slot.alone:
            return slot.given < 1
        return (self._recycle_after is None
                or slot.given < self._recycle_after)

    def _process_of(self, slot):
        """The slot's worker process, captured from the executor the first
        time it is asked for after a `submit()`; None before that."""
        if slot.process is None and not slot.shut:
            processes = getattr(slot.executor, "_processes", None) or {}
            values = []
            for _ in range(3):
                try:
                    values = list(processes.values())
                    break
                except RuntimeError:  # the dict changed while it was read
                    continue
            started = [p for p in values if getattr(p, "pid", None)]
            if started:
                slot.process = started[0]
                self._started(slot.process)
        return slot.process

    def _open(self, alone=False):
        slot = _Slot(new_spawn_process_pool(1, disable_gc=self._disable_gc),
                     alone=alone)
        # Recorded before the first `submit()`, which spawns the worker, so
        # an exit that runs while it spawns still finds it.
        self._live.append(slot)
        return slot

    def _submit(self, slot, chunk):
        """Hand `chunk` to `slot`. Returns False when the slot's worker has
        died, which the caller treats as a death; the chunk then counts as
        one it held."""
        slot.given += 1
        try:
            future = slot.executor.submit(_run_chunk, self._func, chunk[2])
        except BrokenProcessPool:
            # A broken executor refuses at once. The chunk never ran, and
            # the stream cannot deliver it now.
            slot.inflight[_LostChunk()] = chunk
            return False
        slot.inflight[future] = chunk
        slot.submitted[future] = time.monotonic()
        future.add_done_callback(slot.returned)
        self._process_of(slot)
        return True

    def _fill(self):
        """Hand out chunks until every worker holds `depth` or the items are
        used up. Returns `[slot]` for the slot whose worker refused a
        chunk, or `[]`."""
        while True:
            if self._confirm and sum(
                    1 for s in self._live if s.alone) < self._width:
                # An item to run again alone goes first, each on a worker of
                # its own that is given nothing else (owner ruling Q8). These
                # workers have a width of their own, the pool's: not the
                # regular one, because with `recycle_after` None no regular
                # worker ever frees a place and the item would wait for
                # good; and not none, because a stopped chunk of C items
                # would then spawn C interpreters at once (a storage stall
                # under ISOCENTER_CHUNKSIZE spawned one per item; review of
                # #673). So at most twice the width are live, beside the
                # workers told to leave.
                piece = self._confirm.popleft()
                slot = self._open(alone=True)
                if not self._submit(slot, piece):
                    return [slot]
                continue
            regular = [s for s in self._live if not s.alone]
            open_slots = [s for s in regular if self._accepting(s)
                          and len(s.inflight) < self._depth]
            slot = min(open_slots, key=lambda s: len(s.inflight),
                       default=None)
            # A new worker before a second chunk to a busy one: spread first.
            open_new = (len(regular) < self._width
                        and (slot is None or slot.inflight))
            if slot is None and not open_new:
                return []
            chunk = self._again.popleft() if self._again else self._next_chunk()
            if chunk is None:
                return []
            if open_new:
                slot = self._open()
            if not self._submit(slot, chunk):
                return [slot]

    # --- retiring ---------------------------------------------------------

    def _retire(self, slot):
        """Tell the slot's worker to leave, and watch it until it has."""
        process = self._process_of(slot)
        # Watched before it leaves `_live`, so an interrupt between the two
        # leaves the worker in at least one list an exit reads; never in
        # neither. The unfinished exit reads both and ends each once.
        if process is not None:
            self._retiring.append((process, time.monotonic()))
        self._live.remove(slot)
        slot.shut = True
        # `wait=False`, always (rule 4). The worker gets the executor's exit
        # sentinel and leaves on its own, running its exit work (`atexit`,
        # coverage's data file).
        slot.executor.shutdown(wait=False)

    def _end_late_leavers(self, now, final=False):
        """Drop the workers told to leave that have left; SIGKILL those still
        running `_BROKEN_POOL_GRACE_S` after they were told, and name them.
        With `final`, wait for the rest first, each to its own deadline."""
        if not self._retiring:
            return
        if final:
            last = max(at for _, at in self._retiring) + _BROKEN_POOL_GRACE_S
            processes = [p for p, _ in self._retiring]
            try:
                _wait_for_sentinels(processes, last)
            finally:
                # On every way out of the wait, Ctrl-C included: kill, then
                # let the interrupt go on (#861, round 2). No `return` here.
                self._kill_late(time.monotonic(), every=True)
            return
        self._kill_late(now, every=False)

    def _kill_late(self, now, every):
        if every:
            # The exit's kill: `_kill_still_running`, so a Ctrl-C in its
            # readiness check still kills every one before it goes on
            # (review of #913).
            late, self._retiring = self._retiring, []
            killed = _kill_still_running(p for p, _ in late)
        else:
            still = set(_still_running_or_all(p for p, _ in self._retiring))
            late = [(p, at) for p, at in self._retiring if p in still
                    and now - at >= _BROKEN_POOL_GRACE_S]
            self._retiring = [(p, at) for p, at in self._retiring
                              if p in still and (p, at) not in late]
            killed = _kill_each(p for p, _ in late)
        if killed:
            # The time measured, not the grace. A log line and no audit row,
            # as #860's ruling Q3 has for a kill: the worker had sent every
            # result. Tests count these lines by "had finished their tasks";
            # the words "recycling pool" and "told to stop" belong to the
            # unfinished exit's line, and must not appear here.
            get_logger().warning(
                "%d worker process(es) had finished their tasks but were "
                "still running %.1f s after they were told to leave, and "
                "were sent SIGKILL (pid %s). A worker cannot leave while a "
                "non-daemon thread is still running or an exit handler does "
                "not return.",
                len(killed), now - min(at for _, at in late),
                ", ".join(str(pid) for pid in killed))

    # --- deaths -----------------------------------------------------------

    def _ended(self, slots):
        """The slots of `slots` whose worker process has ended, read from
        its sentinel.

        A live worker holding no chunk has no future to fail, so this is how
        its death is seen; any death fails the stream, idle or not (owner
        ruling Q1 on #887).
        """
        dead = []
        for slot in slots:
            process = self._process_of(slot)
            if process is None:
                continue
            try:
                if not _still_running([process]):
                    dead.append(slot)
            except (OSError, ValueError):
                dead.append(slot)
        return dead

    @staticmethod
    def _exit_code(process):
        """The dead worker's exit code, read only once its sentinel is ready
        (#861's rule), or None when it cannot be known."""
        if process is None:
            return None
        deadline = time.monotonic() + 5.0
        try:
            multiprocessing.connection.wait([process.sentinel], timeout=5.0)
            code = process.exitcode
            # The manager thread joins the same process as the future fails,
            # and whichever `waitpid` loses reads None for an instant; the
            # winner caches the code on the `Popen` both read.
            while code is None and time.monotonic() < deadline:
                time.sleep(0.01)
                code = process.exitcode
        except (OSError, ValueError):
            return None
        return code

    def _broken(self, dead):
        """The `BrokenProcessPool` for the deaths of the workers of `dead`
        (a list of slots, every one seen in the same wake), its `lost`
        naming the chunks they held. The message names the first."""
        slot = dead[0]
        process = slot.process
        pid = process.pid if process is not None else None
        code = self._exit_code(process)
        held = "pid unknown" if pid is None else f"pid {pid}"
        if code:
            # Its words must not include "recycling pool", "SIGKILL" or
            # "had finished their tasks", by which tests count the two kill
            # lines. A plain `BrokenProcessPool`, never a subclass:
            # `_report_export_failures` writes the class name into the row.
            message = (
                f"A worker process of a pool that recycles its workers ended "
                f"abruptly ({held}, exit code {code}) while the pool was "
                f"running; the work it held cannot finish, so the pool was "
                f"stopped.")
        else:
            # 0 or unknown is never quoted as the cause: a worker whose
            # initializer raises logs it and exits 0.
            message = (
                f"A worker process of a pool that recycles its workers ended "
                f"({held}) before it returned the work it held, while the "
                f"pool was running; a worker whose initializer raises ends "
                f"this way. The work it held cannot finish, so the pool was "
                f"stopped.")
        exc = BrokenProcessPool(message)
        # For #668: `(first item index, chunk)` for each chunk a dead worker
        # held, every worker that died in the same wake included (owner
        # ruling Q-B on #1199), so they can be run again one at a time.
        # Nothing reads it yet.
        exc.lost = tuple(sorted(((start, chunk) for each in dead
                                 for _, start, chunk in each.inflight.values()),
                                key=lambda pair: pair[0]))
        return exc

    # --- the stream -------------------------------------------------------

    def run(self, func, items, chunksize, ordered):
        """Run `func` on every item, yielding each result.

        Args:
            func: The worker function; module scope, it must pickle.
            items: The items, read lazily.
            chunksize (int): Items per task.
            ordered (bool): Yield in submission order rather than as
                workers finish.

        Yields:
            Each result. A task's own exception propagates.

        Raises:
            BrokenProcessPool: A worker died; after every result already
                delivered (on the ordered path, the ones in submission
                order before the first missing).
        """
        if not isinstance(chunksize, int) or chunksize < 1:
            raise ValueError(f"chunksize must be at least 1, not {chunksize!r}")
        self._func = func
        self._yields_values = isinstance(func, _ExceptionAsResult)
        self._chunks = _numbered_chunks(items, chunksize)
        finished = False
        # Seen by `_end_open_slot_pools` at interpreter exit while it runs.
        # Held here, not read again in the `finally`: a generator finalized
        # during module teardown would find the global already None.
        open_pools = _OPEN_SLOT_POOLS
        open_pools.add(self)
        try:
            yield from self._stream(ordered)
            finished = True
        finally:
            try:
                if finished:
                    self._finished_exit()
                else:
                    self._unfinished_exit()
            finally:
                open_pools.discard(self)

    def _stream(self, ordered):
        held = {}        # ordered: sequence -> results, or a task's exception
        outcomes = []
        next_seq = 0

        def sink(sequence, outcome):
            if ordered:
                held[sequence] = outcome
            else:
                outcomes.append(outcome)

        def ready():
            nonlocal next_seq, outcomes
            while next_seq in held:
                outcomes.append(held.pop(next_seq))
                next_seq += 1
            taken, outcomes = outcomes, []
            return taken

        dead = self._fill()
        while not dead:
            futures = [f for s in self._live for f in s.inflight]
            if not futures:
                break
            done, _ = concurrent.futures.wait(
                futures, timeout=_POOL_WATCH_S,
                return_when=concurrent.futures.FIRST_COMPLETED)
            dead = self._settle(done, sink)
            if not dead:
                dead = self._ended([s for s in self._live if not s.inflight])
            if not dead:
                self._stop_overdue(sink)
                self._end_late_leavers(time.monotonic())
                dead = self._fill()
            if dead:
                break
            for outcome in ready():
                if isinstance(outcome, BaseException):
                    raise outcome
                yield from outcome
        if not dead:
            return
        # Every other worker found ended by now died in the same wake: its
        # futures may not be failed yet (the executor's manager thread marks
        # them), but the chunks it held are lost all the same (owner ruling
        # Q-B on #1199).
        dead += self._ended([s for s in self._live if s not in dead])
        # Every result another worker had already returned is delivered
        # before the death is raised, whichever way the death was seen (a
        # future, an idle worker's sentinel, a refused submit); on the
        # ordered path only those before the first missing one, because
        # `import_files` reads "delivered" as a prefix.
        others = {f for s in self._live if s not in dead
                  for f in s.inflight if f.done()}
        self._settle(others, sink, deaths=False)
        for outcome in ready():
            if isinstance(outcome, BaseException):
                raise outcome
            yield from outcome
        raise self._broken(dead)

    def _settle(self, done, sink, deaths=True):
        """Take each finished future in `done` off its slot and hand its
        results, or its task's exception, to `sink(sequence, outcome)`.
        Retires each slot that has run its quota and holds nothing.

        Returns:
            Every slot whose worker died, in `_live` order; `[]` if none.
            With `deaths=False` a dead worker's future is left where it is,
            and `[]` is returned.
        """
        dead = []
        for slot in list(self._live):
            for future in [f for f in slot.inflight if f in done]:
                exc = future.exception()
                if isinstance(exc, BrokenProcessPool):
                    if deaths and slot not in dead:
                        dead.append(slot)
                    continue
                sequence, _, _ = slot.inflight.pop(future)
                slot.submitted.pop(future, None)
                self._deliver(sequence,
                              exc if exc is not None else future.result(),
                              sink)
            if (slot not in dead and not slot.inflight
                    and not self._accepting(slot)):
                self._retire(slot)
        return dead

    # --- the time bound (#673) ----------------------------------------------

    def _deliver(self, sequence, outcome, sink):
        """Hand a chunk's outcome to `sink`; an item run alone is gathered
        into its chunk's sequence first, and handed over when the last of
        them is in. A task's exception settles the whole chunk at once."""
        if not isinstance(sequence, tuple):
            sink(sequence, outcome)
            return
        _, whole, index = sequence
        held = self._pieces.get(whole)
        if held is None:     # already settled by an exception
            return
        if isinstance(outcome, BaseException):
            del self._pieces[whole]
            sink(whole, outcome)
            return
        held["results"][index] = outcome[0]
        held["left"] -= 1
        if not held["left"]:
            del self._pieces[whole]
            sink(whole, held["results"])

    def _stop_overdue(self, sink):
        """Stop every worker whose running chunk is past the bound."""
        if self._bound is None:
            return
        now = time.monotonic()
        for slot in list(self._live):
            head = next(iter(slot.inflight), None)
            if head is None or isinstance(head, _LostChunk) or head.done():
                continue
            # The later of its handover and its predecessor's return: a
            # chunk queued behind another in the same worker is not running
            # yet, and its wait is not its own.
            began = max(slot.submitted.get(head, now), slot.last_done)
            if now - began >= self._bound:
                self._stop(slot, head, now - began, sink)

    def _stop(self, slot, head, ran, sink):
        """SIGKILL the slot's worker, whose chunk `head` ran `ran` seconds:
        that chunk's items go to be run again alone, or, run alone already,
        are reported; any other chunk it held never started, and is run
        again as it was."""
        process = self._process_of(slot)
        killed = _kill_each([process]) if process is not None else []
        # Watched like a worker told to leave, so the exits and the late
        # kill see it until its sentinel says it has gone.
        if process is not None:
            self._retiring.append((process, time.monotonic()))
        self._live.remove(slot)
        slot.shut = True
        held = list(slot.inflight.items())
        slot.inflight.clear()
        slot.submitted.clear()
        again = []
        first_alone = False
        for future, chunk in held:
            if future.done() and not isinstance(future.exception(),
                                                BrokenProcessPool):
                # It returned as the kill was sent; it is not overdue.
                self._deliver(chunk[0], future.exception() or future.result(),
                              sink)
            elif future is not head:
                again.append(chunk)
            elif slot.alone:
                strikes = self._strikes.pop(chunk[0], 0) + 1
                if strikes < 2:
                    # Its first stop of its own: it may never have started
                    # in the chunk that was stopped. Once more, alone.
                    first_alone = True
                    self._strikes[chunk[0]] = strikes
                    self._confirm.append(chunk)
                    continue
                item = chunk[2][0]
                marker = _TaskDidNotFinish(item, self._bound)
                self._deliver(chunk[0],
                              [marker] if self._yields_values else marker,
                              sink)
            else:
                sequence, start, items = chunk
                self._pieces[sequence] = {"results": [None] * len(items),
                                          "left": len(items)}
                charged = 1 if len(items) == 1 else 0
                for index, item in enumerate(items):
                    piece = ("piece", sequence, index)
                    self._strikes[piece] = charged
                    self._confirm.append((piece, start + index, [item]))
        self._again.extendleft(reversed(again))
        try:
            # After the kill, so the manager thread has no live worker to
            # join while it holds the lock `shutdown()` takes.
            slot.executor.shutdown(wait=False, cancel_futures=True)
        except Exception:  # pylint: disable=broad-exception-caught
            pass
        pids = ", ".join(str(pid) for pid in killed) or "unknown"
        # Neither "after the pool was told to stop" nor "had finished their
        # tasks": tests count the two exit lines by those words.
        if first_alone:
            # An item of a stopped chunk of several, run alone: the chunk's
            # stop was charged to none of its items, so this is its first.
            get_logger().warning(
                "A task ran for %.1f s, past the %g s bound "
                "(ISOCENTER_TASK_TIMEOUT), alone on a fresh worker process, "
                "after the chunk of several it was in had been stopped; that "
                "worker was sent SIGKILL (pid %s), and the task is run once "
                "more, alone, on another fresh worker process before it is "
                "reported (#673).",
                ran, self._bound, pids)
        elif slot.alone:
            get_logger().warning(
                "A task ran for %.1f s, past the %g s bound "
                "(ISOCENTER_TASK_TIMEOUT), a second time, alone on a fresh "
                "worker process; that worker was sent SIGKILL (pid %s), and "
                "the task is reported as not finished (#673).",
                ran, self._bound, pids)
        else:
            get_logger().warning(
                "A task ran for %.1f s, past the %g s bound "
                "(ISOCENTER_TASK_TIMEOUT); its worker process was sent "
                "SIGKILL (pid %s), and each of its items is run once more, "
                "alone, on a fresh worker process before any is reported "
                "(#673).", ran, self._bound, pids)

    # --- exits ------------------------------------------------------------

    def _finished_exit(self):
        """Every result was read: tell every worker to leave, and end, after
        its grace, each one that has not."""
        for slot in list(self._live):
            self._retire(slot)
        self._end_late_leavers(time.monotonic(), final=True)

    def _unfinished_exit(self):
        """Any other way out: a reader that stops, a task's exception, a
        death, Ctrl-C. SIGTERM every worker, wait at most
        `_BROKEN_POOL_GRACE_S`, SIGKILL each one still running and name
        them in one WARNING, then shut every executor down."""
        # Nothing left to end: return before anything else is read. The
        # interpreter-exit hook (`_end_open_slot_pools`) ends an open pool,
        # and the stream's own `finally` then runs this again during module
        # teardown, when this module's globals (`time` among them) may
        # already be None (review of #1199, finding 3).
        if not self._live and not self._retiring:
            return
        processes = [self._process_of(s) for s in self._live]
        processes += [p for p, _ in self._retiring]
        # A worker whose spawn was interrupted may be in `_processes`
        # without having been captured.
        for slot in self._live:
            if slot.process is None:
                processes += list((getattr(slot.executor, "_processes", None)
                                   or {}).values())
        unique = {}
        for process in processes:
            if process is not None:
                unique.setdefault(id(process), process)
        processes = list(unique.values())
        self._retiring = []
        started = time.monotonic()
        try:
            try:
                for process in processes:
                    try:
                        process.terminate()
                    except (OSError, ValueError, AttributeError):
                        continue
                _wait_for_sentinels(processes, started + _BROKEN_POOL_GRACE_S)
            finally:
                # On every way out of the wait, and Ctrl-C is one: kill,
                # then let it go on. A `finally`, never an `except`, and no
                # `return` in it (#861, round 2).
                killed = _kill_still_running(processes)
                if killed:
                    # The time measured, not the grace: an interrupt ends
                    # the wait early. Tests count these lines by "after the
                    # pool was told to stop".
                    get_logger().warning(
                        "%d worker process(es) of the recycling pool were "
                        "still running %.1f s after the pool was told to "
                        "stop, and were sent SIGKILL (pid %s). A worker "
                        "outlives SIGTERM when something in it handles that "
                        "signal; a handler a script installs at module level "
                        "runs again in every spawned worker.",
                        len(killed), time.monotonic() - started,
                        ", ".join(str(pid) for pid in killed))
        finally:
            # After the kill, so no executor has a worker left for its
            # manager thread to join while it holds the lock `shutdown()`
            # takes.
            for slot in self._live:
                if slot.shut:
                    continue
                slot.shut = True
                try:
                    slot.executor.shutdown(wait=False, cancel_futures=True)
                except Exception:  # pylint: disable=broad-exception-caught
                    continue
            self._live = []


class _LostChunk:  # pylint: disable=too-few-public-methods
    """Stands in for the future of a chunk a broken executor refused, so
    the chunk is named in `lost` with the ones the dead worker held."""

    def done(self):
        return False


def _numbered_chunks(items, chunksize):
    """Yield `(sequence, first item index, chunk)` from `items`, lazily."""
    iterator = iter(items)
    sequence = 0
    start = 0
    while True:
        chunk = list(itertools.islice(iterator, chunksize))
        if not chunk:
            return
        yield sequence, start, chunk
        sequence += 1
        start += len(chunk)


#: Every `_SlotPool` whose stream is open, for `_end_open_slot_pools`.
_OPEN_SLOT_POOLS = weakref.WeakSet()


def _end_open_slot_pools():
    """At interpreter exit, end every recycling pool whose stream is still
    open, as its own unfinished exit would (owner ruling Q-A on #1199).

    A stream left open at exit (Ctrl-C or an uncaught exception in the
    caller's loop, whose traceback keeps the generator alive) never reaches
    its `finally` before `concurrent.futures`' own exit hook joins every
    executor's manager thread, which waits for every task in flight and,
    with a worker that cannot leave, for good.
    """
    for pool in list(_OPEN_SLOT_POOLS):
        try:
            pool._unfinished_exit()  # pylint: disable=protected-access
        except Exception:  # pylint: disable=broad-exception-caught
            continue


# Registered after `concurrent.futures.process` is imported (the
# `BrokenProcessPool` import above), because `threading._shutdown` runs these
# hooks last-registered first: this one must run before that module's
# `_python_exit`, which joins the manager threads. `threading._register_atexit`
# is private; it is the only hook that runs before those joins (`atexit` runs
# after them), and it is the one `concurrent.futures` itself uses.
# `test_the_recycling_pool_is_a_slot_pool.py` runs a real interpreter exit
# with a stream left open on each interpreter the gate runs.
threading._register_atexit(_end_open_slot_pools)  # pylint: disable=protected-access


def _run_on_recycling_pool(func, items, strategy, ordered=False,
                           task_timeout=None):
    """Runs on one-worker spawned pools, each replaced after N tasks.

    Args:
        func: The worker function.
        items: The items to process.
        strategy (_Strategy): Its worker count, `maxtasksperchild`,
            `disable_gc`, `chunksize` and progress settings apply.
        ordered (bool): Yield in submission order rather than as workers
            finish.
        task_timeout (Optional[float]): `_SlotPool`'s time bound.

    Yields:
        Each result, as workers finish unless `ordered`.
    """
    # For the imaging paths, where the C libraries behind decoding and
    # compression leak steadily: replacing the process is what gives the
    # memory back. `_SlotPool` says why it is built the way it is.
    #
    # Unordered unless asked: results are yielded as workers finish, so one
    # slow item does not hold back everything queued behind it.
    # `import_files` asks, because the order it links files in decides
    # which of two files sharing an SOP Instance UID is kept, and arrival
    # order would make that a matter of scheduling. `export()` does not:
    # its results are small and order-free.
    pool = _SlotPool(strategy.max_workers,
                     recycle_after=strategy.maxtasksperchild,
                     disable_gc=strategy.disable_gc,
                     task_timeout=task_timeout)
    stream = pool.run(func, items, strategy.chunksize, ordered)
    try:
        yield from _tracked(stream, items, strategy)
    finally:
        # `tqdm` does not pass `close()` on to what it wraps, so a reader
        # that stops would leave the pool's exit to whenever the stream is
        # collected.
        stream.close()


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
    if strategy.use_threads:
        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=strategy.max_workers)
    else:
        # This is the pool that pickles the store, so it is the one #220's
        # spawn pin was for; `new_spawn_process_pool` holds it.
        executor = new_spawn_process_pool(strategy.max_workers,
                                          disable_gc=strategy.disable_gc)

    with executor:
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
    ordered: bool = False,  # Submission order on the recycling pool too
    task_timeout: Optional[float] = None  # Seconds per task (#673)
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
            worker that dies while the pool runs (killed, or its
            initializer raised) fails the pool with `BrokenProcessPool`,
            yielded last after the results already delivered: at once
            when it held a task, within about `_POOL_WATCH_S` (1 s) when
            it held none (#887, #885). A worker told to leave after its
            last task that is still running `_BROKEN_POOL_GRACE_S` (10 s)
            later is sent SIGKILL by the parent and named in a WARNING log
            line (#888, #885). On any way out other than reading every
            result, each worker is sent SIGTERM, and one still running
            `_BROKEN_POOL_GRACE_S` later is sent SIGKILL and named in one
            WARNING log line (#860).
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
        task_timeout (float, optional): Seconds one task may run before
            its worker process is stopped (#673); None, the default, is no
            bound. The caller passes `resolve_task_timeout()`; only
            `ingest()` and `export()` do. On the recycling pool a chunk
            past it is stopped and each of its items run once more alone
            on a fresh worker, and an item past it again comes back as a
            `_TaskDidNotFinish` (yielded under `yield_exceptions`, raised
            otherwise). On a shared `ProcessPoolExecutor` every worker is
            sent SIGKILL when the reader has waited on one result for
            longer, and the stream fails with `_RanPastTheBound`, a
            `BrokenProcessPool`. Not applied to threads, which cannot be
            ended, nor to a pool this call builds without
            `maxtasksperchild`.

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
        results = _run_on_shared_executor(executor, func, items, strategy,
                                          task_timeout)
    elif strategy.maxtasksperchild is not None:
        results = _run_on_recycling_pool(func, items, strategy, ordered,
                                         task_timeout)
    else:
        results = _run_on_new_executor(func, items, strategy)

    if yield_exceptions:
        results = _trailing_exception(results)

    # Each path is a generator, so nothing has run yet. Streaming callers
    # get it untouched; everyone else gets the work done here.
    return results if return_generator else list(results)

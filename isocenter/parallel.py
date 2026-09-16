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


class _Choice(NamedTuple):
    """What the threads-or-processes ranking decided, and who asked.

    `_resolve_execution_choice` returns three facts where it used to
    return one bool, because the bool is not enough to report on: a
    caller that wants to say *why* it is running the way it is would
    otherwise have to rank the levers a second time, and a second
    implementation of the order is a second thing that can disagree with
    it (#384, #400).

    `processes_requested_by` names the lever that **asked** for
    processes, whether or not it got them, and is `None` when nobody
    asked. Two different silences share that `None` and both are
    deliberate: nothing was set at all, and the operator's own
    `ISOCENTER_FORCE_THREADS` superseded their `ISOCENTER_FORCE_PROCESSES`
    by the documented order -- in which case their effective request was
    threads and nothing has been denied. A *default* is likewise not a
    request: on a GIL build with no lever set the ranking ends in
    processes and this field is `None`, which is the single line that
    keeps `Session(":memory:")` working out of the box on the floor
    interpreter.

    `threads_request_overridden_by` names the threads lever that lost to
    worker recycling -- `"ISOCENTER_FORCE_THREADS"` or
    `"force_threads=True"` -- and is `None` otherwise. It carries what
    #185's warning needs so the warning can be emitted at *dispatch*
    rather than here; see `_resolve_execution_choice`.

    `threads_requested_by` names the threads lever that asked **and
    won** -- `"ISOCENTER_FORCE_THREADS"` or `"force_threads=True"`, the
    variable taking the name when both are set, as it does above -- and
    is `None` otherwise: when nobody asked, when recycling beat the
    request (the field above covers that, and #185's warning is the one
    line), and for the free-threaded default, which resolves to threads
    without anyone asking. `DicomImporter.import_files` reads it to say
    that `ISOCENTER_FORCE_THREADS` had no effect on an `ingest()`, which
    runs on the session's process pool whatever the strategy says
    (#393). It is last and defaulted so a positional three-field
    construction still means what it meant.
    """
    use_threads: bool
    processes_requested_by: Optional[str]
    threads_request_overridden_by: Optional[str]
    threads_requested_by: Optional[str] = None


@dataclass(frozen=True)
class _Strategy:  # pylint: disable=too-many-instance-attributes
    # Eight settings and three attribution fields, each read by name by a
    # caller that reports on the decision (#384, #400, #393). Grouping
    # them to satisfy the count would hide which fields exist -- the
    # reason `_resolve_strategy` keeps one parameter per knob.
    """How one `run_parallel` call will actually be executed.

    Resolved once, before any work starts, so the three execution paths
    below read settings rather than each deriving their own.

    The three attribution fields carry `_Choice`'s answer out to callers
    that report on the decision. Two do. `redact()` resolves a strategy
    itself, prints the parenthetical on `Executing using N workers
    (...)` from `use_threads`, and reads `processes_requested_by` to
    decide whether an operator's lever was ignored (#384, #400).
    `DicomImporter.import_files` resolves one for `ingest()` and reads
    `threads_requested_by` to say that a threads lever had no effect on
    the session's process pool (#393). `run_parallel` reads
    `threads_request_overridden_by` for #185's warning.
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

        Threads share the parent's interpreter, so disabling GC or
        arming a process-lifetime faulthandler watchdog in a thread
        would apply to the whole program rather than to a worker.
        """
        if self.use_threads:
            return None
        return resolve_worker_initializer(self.disable_gc)


def _env_int(name: str, *, minimum: Optional[int]) -> Optional[int]:
    """Reads an integer tuning variable, or None if unset or unusable.

    A malformed value is reported rather than dropped. It used to be
    swallowed by a bare `except ValueError: pass`, so a typo in
    `ISOCENTER_MAX_WORKERS` silently reverted to the default and the only
    symptom was a cohort running at the wrong width.

    A value below `minimum` is reported the same way and dropped the
    same way (#341). The floor lives here and not at the call sites
    because a floor at a call site is a floor at *one of the places* a
    variable is read: #335 guarded `ISOCENTER_MAX_WORKERS` where
    `_resolve_strategy` reads it, and `_redaction_worker_count` in
    `session.py` went on reading the same variable and clamping `0` to a
    single worker in silence. `ISOCENTER_CHUNKSIZE` meanwhile kept the
    `or` both fixed arms had removed. Four reads, one helper, one floor.

    `minimum` is keyword-only and has **no default**, on purpose. A
    default of `1` would make the floor invisible at the read site and
    turn a future variable that legitimately accepts `0` into a silent
    rejection; a default of `None` would let a fifth read site inherit
    #335's defect by omission. Every read states its floor, or states
    `minimum=None` and is seen to.

    On rejection this returns `None` and **never `0`**, so a caller's
    `is not None` is the only test it needs. `or` is the spelling that
    got this wrong twice (#335, #341): `0` is the one value that must be
    reported and the one value that is falsey, so `_env_int(...) or
    default` discards exactly the value it should be shouting about, and
    passes a negative -- truthy -- straight through to a pool constructor
    that raises naming no environment variable.
    """
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
        # `0` and every negative in one arm, so there is not one
        # behaviour for `0` and another for `-1`: neither is a usable
        # value and an operator who typed either made the same mistake.
        # The value is quoted as well as the variable -- a message that
        # does not say what was rejected cannot be matched against what
        # was typed. The per-variable advice ("set it to 1 for a single
        # worker") lives in the docs/environment.md rows, not here: two
        # hand-written tails for one behaviour were two spellings of it.
        get_logger().warning(
            "%s is set to %d, which is below the minimum of %d. Ignoring "
            "it and using the default.", name, value, minimum)
        return None
    return value


def _env_is(name: str, values) -> bool:
    """Whether an environment variable is set to one of `values`."""
    return os.environ.get(name, "").lower() in values


def progress_enabled(show: bool = True) -> bool:
    """Whether a progress bar is drawn: the caller's `show`, unless
    `ISOCENTER_SHOW_PROGRESS` switches it off.

    The one spelling of that rule (#540). It was written once, inside
    `_resolve_strategy`, so it reached the bars `run_parallel` draws and
    no other: `anonymize()`, `release_memory()` and `lock_identities()`
    each imported `tqdm` themselves and drew with the variable at `0`.
    Read per call, not cached, so a variable set after import applies.
    """
    return bool(show) and not _env_is("ISOCENTER_SHOW_PROGRESS", _FALSEY)


def progress_bar(iterable=None, *, show: bool = True, **kwargs):
    """`tqdm`, drawn only when `progress_enabled(show)`.

    Every bar outside `run_parallel` goes through here, and
    `tests/test_progress_bars_honour_the_environment.py` fails if a module
    other than this one imports `tqdm`: a second import is a bar the
    variable does not reach. It calls this module's `tqdm` global, so a
    test that patches `isocenter.parallel.tqdm` sees these bars too.
    """
    return tqdm(iterable, disable=not progress_enabled(show), **kwargs)


def resolve_max_workers() -> int:
    """The default worker count: `ISOCENTER_MAX_WORKERS`, else one per CPU.

    The one place that expression is computed (#333). `_resolve_strategy`
    calls it when no `max_workers` argument is given, and `Session` calls
    it to size its shared pool and every restart of that pool (#501).
    Until #501 the shared pool was built with `max_workers=None` and the
    stdlib chose its width, so the variable narrowed every `run_parallel`
    call and `redact()`, but never the pool `ingest()` runs on. It is a
    helper rather than a `_resolve_strategy` call because the session
    needs this one number, and resolving a whole strategy at `Session()`
    would read five other variables and emit their warnings when the
    session opens.

    One worker per CPU, not the 1.5x an earlier version used:
    predictable beats marginally faster when a run is hours long. A value
    below 1 is reported by `_env_int` and dropped, so this never returns
    less than 1 (#335, #341).
    """
    configured = _env_int("ISOCENTER_MAX_WORKERS", minimum=1)
    return configured if configured is not None else (os.cpu_count() or 1)


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
    # The three integer variables below share one floor, stated at each
    # read (the worker count's is in `resolve_max_workers`, which the
    # session's shared pool reads as well, #501):
    # `_env_int(..., minimum=1)` reports `0` and every negative and
    # returns `None`, so the `is not None` fallbacks here see a rejected
    # value exactly as they see an unset or malformed one (#335, #185,
    # #341 -- the docstring on `_env_int` has the history, and why the
    # spelling is never `_env_int(...) or default`).
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
    # #185 warning already makes for the threads side.
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

    Worker recycling has the last word: on 3.12, the floor, only
    `multiprocessing.Pool` recycles workers (`ProcessPoolExecutor`'s
    `max_tasks_per_child`, new in 3.11, deadlocks `map` on 3.12 at the
    first replacement -- measured in the review of #504), so asking for
    it rules threads out however the rest of the environment is set. This is the whole of the
    precedence, and the whole of it lives here: any second reading of
    these variables anywhere else would be a second copy of the order,
    which is the defect class #384 and #400 are both instances of.

    **This function emits nothing.** It used to announce #185's
    recycling override itself, and could not go on doing so once
    `redact()` began resolving a strategy *before* deciding whether to
    run at all: the one configuration it refuses is exactly the one
    whose resolution warned "so this run uses processes", which would
    have put that sentence one line above a refusal of a run that never
    starts. The attribution travels on `_Choice` instead and
    `run_parallel` speaks at dispatch, where the strategy is used.
    Frequency is unchanged for every path that exists today: resolution
    and dispatch are one-to-one inside `run_parallel`.

    `recycling_lever` is which spelling supplied `maxtasksperchild` --
    `"ISOCENTER_MAX_TASKS_PER_CHILD"` or `"the maxtasksperchild
    argument"`. `_resolve_strategy` is the only caller that knows, so it
    passes it in rather than this function reading the environment a
    second time to find out.
    """
    forced_by_env = _env_is("ISOCENTER_FORCE_THREADS", ("1",))
    processes_by_env = _env_is("ISOCENTER_FORCE_PROCESSES", ("1",))

    # Attribution is settled on the way *through* the ranks and before
    # any of them short-circuits, which is the whole trick. Computed
    # after the fact -- from the resolved `use_threads`, say -- it would
    # be `None` on every `redact()` call against a `:memory:` store,
    # because that path passes `force_threads=True` and so always ends
    # in threads however the environment is set. That is precisely the
    # path whose ignored lever #400 is about.
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
        # place that can name who asked (#393). Not derived from
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
    """Says that worker recycling beat a request for threads (#185).

    Emitted at **dispatch**, from `run_parallel`, rather than where the
    ranking is resolved: a strategy that is resolved and then not run
    has nothing to report, and `redact()` is the one caller that can
    resolve without running. See `_resolve_execution_choice`.

    Fires when, and only when, threads were actually asked for.
    `session.export()` passes `maxtasksperchild=25` on every export and
    asks for nothing else, so the ordinary path is silent; a line on
    every export would be noise that teaches readers to filter this
    logger.
    """
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


def _run_on_recycling_pool(func, items, strategy, ordered=False):
    """Runs in a pool whose workers are replaced every N tasks.

    This exists for the imaging paths, where the C libraries behind
    decoding and compression leak steadily. `ProcessPoolExecutor` has had
    `max_tasks_per_child` since 3.11, but on 3.12 -- the floor -- its `map`
    deadlocks the first time a worker is replaced (3.12.14, spawn; 3.14
    and 3.14t are fine). So the older `multiprocessing.Pool` is the only
    way to get memory back during a long run on every supported
    interpreter. Do not "modernise" this to the executor kwarg while 3.12
    is supported (#501).

    Spawn, not fork: a forked worker inherits the parent's open SQLite
    handles and its sidecar file position.
    """
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(processes=strategy.max_workers,
                  maxtasksperchild=strategy.maxtasksperchild,
                  initializer=strategy.worker_initializer) as pool:
        # Unordered unless asked: results are yielded as workers finish,
        # so one slow item does not hold back everything queued behind
        # it. `import_files` asks, because the order it links files in
        # decides which of two files sharing an SOP Instance UID is kept
        # (#431), and arrival order made that a matter of scheduling
        # (#450). `export()` does not: its results are small and
        # order-free, and holding them behind a slow instance buys
        # nothing.
        mapper = pool.imap if ordered else pool.imap_unordered
        iterator = mapper(func, items, chunksize=strategy.chunksize)
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
    table, including the order the three threads-or-processes levers resolve in
    (`_resolve_execution_choice` is where that order lives).

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
            receive an exception as data (#232). `ingest()` goes further
            with the trailing failure than reporting it: when it is a dead
            worker, `io_handlers._ingest_results` reads the files not yet
            returned again and names the one that ends a worker (#654).
            One promise the recycling
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
            already. `redact()` is the one caller that does, so that the
            strategy it *names* on the console and the pool it *gets*
            are two readings of one object rather than two resolutions
            that can disagree (#384). `import_files` does too, so that
            #393's warning reads the strategy the dispatch uses.
            `executor`, `return_generator`, `yield_exceptions` and
            `ordered` are not resolution settings and still apply.
        ordered (bool): If True, results come back in submission order
            on the recycling pool (`maxtasksperchild`), which otherwise
            yields them as workers finish. The shared-executor path
            (`executor.map`, or a Pool's `imap`) and the per-call
            executor path (`executor.map`) are ordered already, so it
            changes nothing there. `import_files` passes it, because
            the order it links files in decides which duplicate is kept
            (#450); `export()` does not.

    Returns:
        Union[List[R], Iterator[R]]: The results of the parallel execution.
    """
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
    # #185 override exactly as one resolved here does -- and so a
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

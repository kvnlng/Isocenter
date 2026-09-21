import collections
import faulthandler
import itertools
import multiprocessing
import os
import signal
import sys
import threading
import time

import pytest
import numpy as np
import warnings

# Suppress all pydicom warnings during tests
warnings.filterwarnings("ignore", module="pydicom.*")

import os
import json
from datetime import date
from isocenter.entities import Patient, Study, Series, Instance, Equipment
from isocenter.builders import DicomBuilder
from support import root_guard
from support import shards

# ---------------------------------------------------------------------
# Stall watchdog (#250) -- instrumentation, not a fix
# ---------------------------------------------------------------------
#
# **What pytest's own faulthandler does not cover.** `faulthandler_timeout`
# is armed by `_pytest/faulthandler.py` inside `pytest_runtest_protocol`
# and cancelled in that function's `finally`, so it covers setup, call and
# teardown of one item and nothing else: not collection, not the gap
# between items, not `pytest_sessionfinish` or the unconfigure chain, and
# not the `atexit` phase that runs after the summary line. #250's
# signature -- a run killed by an outer cap with no failing test named --
# is what a hang in any of those looks like, and the `atexit` phase is
# where this suite already spends real time.
#
# **A background diagnostic that writes to `sys.stderr` does not exist.**
# Measured: pytest's fd-level global capture has already `dup2`-ed over
# fd 2 by the time `tests/conftest.py` is imported, so an `os.dup(2)`
# taken at import time lands on the capture temp file -- a direct write to
# it produced no output at all in the run's log. The same measurement
# showed the channel that does work: global capture is *suspended* while
# `pytest_configure` runs, so `os.dup(2)` taken there is the real stderr,
# and three background writes from a daemon thread appeared live and in
# order in the run's output. Nothing private to `_pytest` is touched. The
# rule this encodes: **a background diagnostic channel must own an fd
# duplicated while capture is not in place, or it is not a channel.**
#
# **Two hooks for the hang probe (`.github/workflows/hang-probe.yml`),
# installed in `pytest_configure` beside the watchdog.** First, SIGUSR1 is
# registered with `faulthandler` on the same fd: pytest's own faulthandler
# registers the fatal signals (SEGV, FPE, ABRT, BUS, ILL) and nothing else,
# so a TERM from an outer timeout produces no dump at all -- the issue's
# retraction measured exactly that -- and the probe needs a signal it can
# send at its deadline that leaves every thread's stack in the log.
# Unconditional: it is diagnostics, costs nothing, and settles the "a TERM
# produced no dump" confusion for anyone else who reaches for one. Second,
# when `ISOCENTER_HANG_PROBE_START_METHOD` is exactly `fork`,
# `multiprocessing.get_context` is rebound so a request for `"spawn"`
# returns the fork context. All four pool pins (`parallel.py`'s two pools,
# `Session._executor` and its OOM restart) are the literal
# `multiprocessing.get_context("spawn")` read through the module attribute
# at construction time, so the rebinding reaches `ProcessPoolExecutor(
# mp_context=...)`, `ctx.Pool(...)` and the session executor alike, and
# the probe's fork arm re-creates the population #260 removed. Any other
# value, or none, is inert. **This variable is test-only and must stay
# so**: `tests/test_documented_env_vars.py` sweeps `isocenter/` for reads
# and demands a `docs/environment.md` row for each, and none is written
# here because the registry documents levers the package reads -- a
# package-level way to pick fork would bring that population back for
# users, which is the thing #260 removed. If the read ever moves into
# `isocenter/`, that sweep goes red and the row and its registry test
# move with it. `tests/test_hang_probe_hooks.py` holds both hooks.

#: How long nothing may happen before the watchdog says so.
#:
#: Must exceed the longest legitimate gap in a healthy run. The worst was
#: `test_persistence_chaos`'s 30.01s teardown, now ~0s (#250's worker
#: leak). 120s is clear of anything the suite does and far inside CI's
#: Run Tests step cap, which `tests/test_packaging_contract.py` pins.
_STALL_S = 120.0

#: How often the watchdog looks. Cheap: one wakeup and one subtraction.
_TICK_S = 10.0

_stderr_fd = None
#: Held at module scope for the process's lifetime. A file object over the
#: dup'ed fd that got collected would close the fd out from under
#: `faulthandler`, whose `file=` keeps only the integer.
_stderr_file = None

_phase_lock = threading.Lock()
_phase = "starting"
_last_nodeid = None
_last_event = time.monotonic()
_started = time.monotonic()


def _mark(phase, nodeid=None):
    global _phase, _last_nodeid, _last_event
    with _phase_lock:
        _phase = phase
        if nodeid is not None:
            _last_nodeid = nodeid
        _last_event = time.monotonic()


def _dump_stall(stalled_for):
    """Say where the run is stuck, on a channel capture cannot swallow."""
    with _phase_lock:
        phase, nodeid, since = _phase, _last_nodeid, _last_event
    names = collections.Counter(t.name for t in threading.enumerate())
    try:
        children = [(c.name, c.pid) for c in multiprocessing.active_children()]
    except Exception as exc:  # pragma: no cover - diagnostics never raise
        children = [("<unavailable>", repr(exc))]

    lines = [
        "",
        "=" * 70,
        f"ISOCENTER STALL WATCHDOG: nothing has happened for "
        f"{stalled_for:.0f}s (#250)",
        f"  phase          : {phase}",
        f"  last test item : {nodeid}",
        f"  in that phase  : {time.monotonic() - since:.0f}s",
        f"  run elapsed    : {time.monotonic() - _started:.0f}s",
        f"  threads        : {threading.active_count()} "
        f"{dict(names.most_common())}",
        # A churning child list across successive dumps is a pool
        # respawning; a static one is a child that is stuck.
        f"  child processes: {children}",
        "=" * 70,
        "",
    ]
    try:
        os.write(_stderr_fd, ("\n".join(lines)).encode())
        # `all_threads` covers this process only; pool children arm their
        # own watchdog (`parallel._worker_init`).
        faulthandler.dump_traceback(all_threads=True, file=_stderr_file)
        _stderr_file.flush()
    except Exception:  # pragma: no cover - diagnostics never raise
        pass


def _watch():
    """Report every `_STALL_S` *while still stalled*, not once.

    A series of dumps is what separates a deadlock (identical stacks) from
    a livelock (stacks that move), and a 40-minute hang should leave a
    series rather than a single frame.
    """
    next_report = _STALL_S
    while True:
        time.sleep(_TICK_S)
        with _phase_lock:
            idle = time.monotonic() - _last_event
        if idle >= next_report:
            _dump_stall(idle)
            next_report = idle + _STALL_S
        elif idle < _STALL_S:
            next_report = _STALL_S


#: The hang probe's only lever (`.github/workflows/hang-probe.yml`). Read
#: here and nowhere in `isocenter/` -- see the comment block above for why
#: that boundary is the point.
_PROBE_START_METHOD_VAR = "ISOCENTER_HANG_PROBE_START_METHOD"


def _install_fork_override():
    """Make every request for a spawn context return the fork context.

    The wrapper delegates every other method unchanged, so only the four
    literal `get_context("spawn")` pins are affected and a caller asking
    for `"forkserver"` or the platform default still gets what it asked
    for. Announced on the watchdog's fd, once, so a probe log says which
    population it measured.
    """
    real_get_context = multiprocessing.get_context

    def forking_get_context(method=None):
        if method == "spawn":
            return real_get_context("fork")
        return real_get_context(method)

    multiprocessing.get_context = forking_get_context
    os.write(_stderr_fd,
             (f"ISOCENTER HANG PROBE: {_PROBE_START_METHOD_VAR}=fork, "
              "multiprocessing.get_context('spawn') now returns the fork "
              "context for every pool in this run (#250)\n").encode())


# Spawned workers inherit the environment but resolve a relative coverage
# data file against *their* cwd, and since #707 that is a tmp_path pytest
# deletes. An absolute COVERAGE_FILE set before the first spawn is what
# every worker then writes beside. Set whether or not coverage is
# running: it is inert without it, and keying it on another variable is
# one more name to be wrong about. Anchored on this file, not on the cwd,
# so a scratch copy of the tree writes into the copy. Respects a
# COVERAGE_FILE the caller set. Every subprocess a test launches inherits
# it too, so a test that runs coverage in a scratch project must strip
# `COVERAGE_*` from the child's environment
# (`tests/test_coverage_keeps_worker_data_under_chdir.py` does).
_TREE_UNDER_TEST = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if not os.environ.get("COVERAGE_FILE"):
    os.environ["COVERAGE_FILE"] = os.path.join(_TREE_UNDER_TEST, ".coverage")

# A `python -c`/`-m` child a test starts without `cwd=` used to find this
# tree through `''` on its `sys.path`, because the cwd was the root. Since
# #707 the cwd is a tmp_path, and the child falls through to whatever
# `isocenter` is installed -- the main checkout's editable install, so in
# a worktree without PYTHONPATH the child tests `main` while this process
# tests the worktree (#720 review). First on PYTHONPATH, anchored like
# COVERAGE_FILE above; anything the caller put there stays after it.
_pythonpath = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
if _pythonpath[:1] != [_TREE_UNDER_TEST]:
    os.environ["PYTHONPATH"] = os.pathsep.join([_TREE_UNDER_TEST] + _pythonpath)
del _pythonpath


#: Set in `pytest_configure` when `--record-shard-timings` is given (#707).
_timing_recorder = None


def pytest_addoption(parser):
    group = parser.getgroup("isocenter")
    group.addoption(
        "--shard", default=None, metavar="I/N",
        help="run only the test files assigned to shard I of N (#707)")
    group.addoption(
        "--record-shard-timings", default=None, metavar="PATH",
        help="write per-file wall time to PATH, for tests/shard_timings.json")


def pytest_collection_modifyitems(config, items):
    """Keep only this shard's files, under `--shard=I/N` (#707)."""
    spec = config.getoption("--shard")
    if not spec:
        return
    try:
        index, count = shards.parse(spec)
    except ValueError as exc:
        raise pytest.UsageError(str(exc)) from None
    repo = config.rootpath
    suite = shards.suite_files(repo)
    # Assigned over the whole suite, never over `items`: a run restricted
    # to two paths must put each file in the shard the full run would.
    mine = set(shards.assign(suite, shards.load_timings(repo), count)[index - 1])
    known = set(suite)
    keep, drop, orphans = [], [], set()
    for item in items:
        try:
            name = item.path.relative_to(repo).as_posix()
        except ValueError:
            name = str(item.path)
        if name not in known:
            orphans.add(name)
        (keep if name in mine else drop).append(item)
    # A collected file the partition does not list is in no shard, so
    # every one of the N jobs would deselect it and pass. Refused, named.
    if orphans:
        raise pytest.UsageError(
            "--shard: collected files that no shard owns (the partition is "
            "tests/test_*.py, tests/support/shards.py): "
            + ", ".join(sorted(orphans)))
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep


def pytest_runtest_logreport(report):
    if _timing_recorder is not None:
        _timing_recorder.add(report.nodeid, report.duration)


def pytest_configure(config):
    """Take the fd, start the watchdog, and arm the probe's two hooks.

    Here rather than at import: global capture is suspended while this
    hook runs, so fd 2 is the real stderr. See the measurement above.

    Everything sits behind the one-configure guard, the fork override
    included: a nested in-process pytest (pytester's `runpytest`) calls
    this hook again, and a second wrapper over the first would still
    answer correctly but would be a stack nobody meant to build.
    """
    global _stderr_fd, _stderr_file, _timing_recorder
    if _stderr_fd is not None:  # pragma: no cover - one configure per run
        return
    if config.getoption("--record-shard-timings"):
        _timing_recorder = shards.TimingRecorder()
    _stderr_fd = os.dup(2)
    # `closefd=False` so the fd survives if this wrapper is ever replaced;
    # the module-level reference is what actually keeps it open.
    _stderr_file = os.fdopen(_stderr_fd, "w", buffering=1, closefd=False)
    _mark("configure")
    # Daemon, so a wedged run is not kept alive by the watchdog itself. It
    # keeps ticking through `pytest_unconfigure` and the whole `atexit`
    # chain -- the half nothing else covers -- and stops when interpreter
    # finalization begins, which is after `atexit`.
    threading.Thread(target=_watch, name="IsocenterStallWatchdog",
                     daemon=True).start()
    # SIGUSR1 -> every thread's stack, on the fd that reaches the log.
    # `chain=False`: there is no prior handler worth calling, and the
    # default disposition would be to die. Guarded because Windows has no
    # SIGUSR1; the probe never runs there.
    if hasattr(signal, "SIGUSR1"):
        faulthandler.register(signal.SIGUSR1, file=_stderr_file,
                              all_threads=True, chain=False)
    if os.environ.get(_PROBE_START_METHOD_VAR) == "fork":
        _install_fork_override()


def pytest_collection(session):
    _mark("collection")


def pytest_runtest_logstart(nodeid, location):
    _mark("running test item", nodeid)


#: The repository root's listing when this run started (#707).
_root_before = None
#: What `pytest_sessionfinish` found, for `pytest_unconfigure` to repeat.
_root_report = None


def pytest_sessionstart(session):
    # One snapshot per run: a nested in-process pytest calls this again,
    # and the inner run's start is not the outer run's baseline.
    global _root_before
    if _root_before is None:
        _root_before = root_guard.snapshot(session.config.rootpath)


def pytest_sessionfinish(session, exitstatus):
    """Fail the run if it wrote into the repository root (#707).

    Every test runs in its own `tmp_path`, so a new or rewritten root
    entry is a write that escaped it -- an absolute path built on
    `__file__`, a wide-scoped fixture's relative write, or a `repo_root`
    test. Named rather than cleaned up: cleanup would hide the next test
    that writes into the tree.
    """
    global _root_report
    _mark("sessionfinish")
    target = session.config.getoption("--record-shard-timings")
    if _timing_recorder is not None and target:
        _timing_recorder.write(target)
    if _root_before is None:
        return
    _root_report = root_guard.report(session.config.rootpath, _root_before)
    if _root_report is not None:
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(_root_report, red=True)
        # Only a clean run is turned red: an interrupt's 2 or a usage
        # error's 4 says more than this does, and a failed run is red
        # already.
        if session.exitstatus == 0:
            session.exitstatus = 1


def pytest_unconfigure(config):
    _mark("unconfigure / atexit")
    # Again, last: pytest prints its warnings, short summary and stats
    # line after `pytest_sessionfinish`, so the line above sits over a
    # green `N passed`, and `RELEASING.md` step 3 records a run by its
    # last line (#720 review). This hook runs after the stats line.
    if _root_report is not None:
        reporter = config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(_root_report, red=True)


@pytest.fixture(scope="session", autouse=True)
def _log_outside_a_test_to_scratch(tmp_path_factory):
    """Where `isocenter.log` goes when no test is running (#707).

    A module- or session-scoped fixture is set up between tests, outside
    any test's `tmp_path` and `redirect_logging`. With no
    `ISOCENTER_LOG_FILE` there, a `Session` it opened logged to
    `./isocenter.log` -- the repository root, which the root guard now
    refuses. `redirect_logging` restores this value rather than deleting
    the variable, so every gap between tests sees it.
    """
    previous = os.environ.get("ISOCENTER_LOG_FILE")
    os.environ["ISOCENTER_LOG_FILE"] = str(
        tmp_path_factory.mktemp("outside-a-test") / "isocenter.log")
    yield
    if previous is None:
        os.environ.pop("ISOCENTER_LOG_FILE", None)
    else:
        os.environ["ISOCENTER_LOG_FILE"] = previous


@pytest.fixture(autouse=True)
def redirect_logging(tmp_path):
    """Redirects isocenter.log to a temp file for all tests."""
    previous = os.environ.get("ISOCENTER_LOG_FILE")
    log_file = tmp_path / "isocenter.log"
    os.environ["ISOCENTER_LOG_FILE"] = str(log_file)
    yield
    if previous is None:
        os.environ.pop("ISOCENTER_LOG_FILE", None)
    else:
        os.environ["ISOCENTER_LOG_FILE"] = previous


@pytest.fixture(autouse=True)
def _own_working_directory(request, tmp_path):
    """Run every test in its own `tmp_path` (#707).

    A relative `Session("foo.db")` used to land in the repository root,
    so two runs in one tree shared `foo.db`, its sidecar and both lock
    files. Spawned workers inherit the cwd, so the pools follow.

    `repo_root` opts out, for a test that builds or launches from the
    root. Do not widen the opt-out to silence a failure: a test that
    breaks here was reading a file an earlier test happened to leave
    behind, and that is the defect.

    `os.chdir` by hand, not `monkeypatch.chdir`: an autouse fixture that
    requests `monkeypatch` instantiates it ahead of every autouse fixture
    defined below this one, so its undo would run *after* their
    teardown. `_pixel_analysis_ocr_is_not_left_replaced` then saw a
    test's own `monkeypatch.setattr` still in place and failed it
    (measured, `test_scan_reports_what_it_could_not_read.py`).
    """
    if request.node.get_closest_marker("repo_root") is not None:
        yield
        return
    started = os.getcwd()
    os.chdir(tmp_path)
    try:
        yield
    finally:
        os.chdir(started)


@pytest.fixture(autouse=True)
def _pixel_analysis_ocr_is_not_left_replaced():
    """Fail the test that leaves `pixel_analysis._ocr_instance` replaced.

    Not its victims. A stand-in left on the module answers every later
    scan: when `test_scan_pixel_findings_name_the_live_graph.py`'s worker
    raced two `mock.patch` calls in threads and left one installed, twelve
    tests in `test_scan_reports_what_it_could_not_read.py` and
    `test_voi_lut_integration.py` went red in CI, none of them the cause
    (review of #466). Checked by code object rather than identity, so
    `test_pydicom_deprecations.py`'s `importlib.reload(pixel_analysis)`,
    which defines a new `_ocr_instance` in place, passes. Torn down after
    `monkeypatch`, which undoes a test's own replacement first.
    """
    yield
    from isocenter import pixel_analysis
    ocr = pixel_analysis._ocr_instance  # pylint: disable=protected-access
    code = getattr(ocr, "__code__", None)
    assert (code is not None and code.co_name == "_ocr_instance"
            and code.co_filename.endswith(
                os.path.join("isocenter", "pixel_analysis.py"))), (
        f"this test left pixel_analysis._ocr_instance replaced by {ocr!r}")


@pytest.fixture
def ocr_present(monkeypatch):
    """OCR that can run, whatever this interpreter has installed (#422).

    `scan_pixel_content()` and `discover_redaction_zones()` refuse up
    front when OCR cannot run, so a test that wants either one to reach
    its pool has to say OCR is present. **Patching `HAS_OCR` alone is not
    enough, and the failure is an environment divergence rather than a
    red test:** the refusal also probes the tesseract binary through
    `pixel_analysis.pytesseract.get_tesseract_version()`, and locally
    `pytesseract` is `None` (neither gate interpreter has it) while CI
    installs the real one. A test patching only the flag goes red here and
    stays green there.

    `image_to_data` answers with no text, so a test that lets the real
    `analyze_pixels` run sees zero regions rather than an `OCR failed`
    ERROR per frame. Tests that need regions patch `analyze_pixels`
    itself. Threads only: `monkeypatch` does not cross a process boundary.
    """
    from types import SimpleNamespace
    from isocenter import pixel_analysis
    stub = SimpleNamespace(
        get_tesseract_version=lambda: "5.5.3",
        image_to_data=lambda *a, **k: {"text": [], "conf": [], "left": [],
                                       "top": [], "width": [], "height": []},
        Output=SimpleNamespace(DICT="dict"),
    )
    monkeypatch.setattr(pixel_analysis, "HAS_OCR", True)
    monkeypatch.setattr(pixel_analysis, "pytesseract", stub)
    return stub


@pytest.fixture
def dummy_pixel_array_2d():
    """A signed frame, deliberately: `int16` is CT and MR.

    This was `uint16` until #404, and `uint16` is one of the exactly two
    dtypes the old JPEG 2000 encoder accepted. Every export test on the
    default compressed path rides this fixture -- 21 references across five
    files -- so the suite was uniformly green while `session.export()`
    wrote **nothing at all** for signed pixel data, failing with
    `Compression failed: broken data stream when writing image file`. A
    shared fixture that only ever exercises the working half of a
    dichotomy is a blind spot, not a default; this one now sits on the
    side that was broken.
    """
    return np.zeros((512, 512), dtype=np.int16)


@pytest.fixture
def dummy_patient(dummy_pixel_array_2d):
    """Creates a full object graph using the Builder."""
    return (
        DicomBuilder.start_patient("P123", "Test^Patient")
        .add_study("1.2.840.111.1", date(2023, 1, 1))
        .add_series("1.2.840.111.1.1", "CT", 1)
        .set_equipment("TestManu", "TestModel", "SN-999")
        .add_instance("1.2.840.111.1.1.1", "1.2.840.10008.5.1.4.1.1.2", 1)
        .set_pixel_data(dummy_pixel_array_2d)

        # Type 1 (Pos/Orient/Spacing)
        .set_attribute("0020,0032", ["0", "0", "0"])
        .set_attribute("0020,0037", ["1", "0", "0", "0", "1", "0"])
        .set_attribute("0028,0030", ["0.5", "0.5"])

        # --- FIX: ADD TYPE 2 MANDATORY TAGS ---
        .set_attribute("0018,0050", "2.5")  # SliceThickness
        .set_attribute("0018,0060", "120")  # KVP

        .end_instance()
        .end_series()
        .end_study()
        .build()
    )

@pytest.fixture
def config_file(tmp_path):
    """Creates a temporary YAML config file."""
    data = {
        "version": "1.0",
        "machines": [
            {
                "serial_number": "SN-999",
                "model_name": "TestModel",
                "redaction_zones": [{"roi": [10, 50, 10, 50]}]
            }
        ]
    }
    p = tmp_path / "rules.yaml"
    import yaml
    with open(p, "w") as f:
        yaml.dump(data, f)
    return str(p)


#: Secondary Capture Image Storage. `reloaded_redaction_session` uses it
#: deliberately -- see that fixture's docstring.
SC_STORAGE = "1.2.840.10008.5.1.4.1.1.7"


@pytest.fixture
def reloaded_redaction_session(tmp_path):
    """A saved-and-reopened session whose pixels arrive read-only.

    Build -> save() -> close() -> reopen, so `get_pixel_data()` comes back
    through `SidecarPixelLoader`, which builds its array with
    `np.frombuffer` over an immutable `bytes` buffer and is therefore
    **not writeable**. That is the ordinary shape for any instance loaded
    from a saved store -- the documented ingest -> save -> reopen ->
    redact workflow -- and until #229 no redaction test in the suite used
    it. Every other fixture builds the graph in memory or reads a source
    file, and both give a *writeable* array, where the redaction path
    mutates in place and is correct. That is the only reason a rule with
    N zones applying only its Nth zone survived the whole suite.

    The `flags.writeable is False` assertion is the fixture's guard on
    itself: give this instance a `file_path` and pydicom hands back a
    writeable array, at which point every test built on it goes vacuous
    rather than red.

    **The instance is exportable, and that is not decoration.**
    `session.export()` does not raise when an instance fails module
    validation -- it logs and writes nothing -- so a fixture missing a
    Type 1 element leaves an empty output tree, and any test that walks
    that tree iterates an empty list and passes. Measured on `4507d48`: a
    CT-class instance without these fails with `['[Type 1 Error] Missing
    0008,0020 in Common', '[Type 2 Error] Missing 0008,0030 in Common',
    '[Type 2 Error] Missing 0018,0050 in CTImage', ...]` (Study Time was
    reported as Type 1 until #495 corrected the validator to PS3.3
    C.7.2.1's Type 2). Under SC Image Storage the CTImage module does not
    apply, which is why only the Common-module elements have to be
    supplied here.

    Yields:
        A callable `make(zones, ...)` returning `(session, instance)`. The
        returned session already carries a rule matching the instance's
        serial. Every session handed out is closed at teardown -- an
        unclosed one leaks worker subprocesses.
    """
    from isocenter.session import DicomSession

    opened = []
    made = itertools.count()

    def make(zones, *, serial="SN_RELOAD", uid=None,
             shape=(32, 32), fill=200, sop_class=SC_STORAGE, name=None):
        # **`name` and `uid` both default per call, and they have to be
        # the same counter.** `name` names the database file and `uid`
        # identifies the instance inside it; two `make()` calls in one
        # test used to share `tmp_path/reload.db` *and* `1.2.3.reload`,
        # so the second reopened the first's store and the `next(...)`
        # below could hand back the first call's instance -- the
        # vacuous-fixture failure this fixture exists to prevent,
        # reintroduced by its own defaults. Deriving only `name` closes
        # it for the default call and leaves it open for the documented
        # one: `make(z, name="x")` twice lands two instances carrying
        # one UID in one database, which is the same collision through
        # the parameter instead of the default.
        n = next(made)
        name = f"reload{n}" if name is None else name
        uid = f"1.2.3.reload{n}" if uid is None else uid
        db_file = tmp_path / f"{name}.db"

        session = DicomSession(str(db_file))
        opened.append(session)
        patient = Patient("P_RELOAD", "Test^Patient")
        study = Study(f"ST_{name}", "20230101")
        series = Series(f"SE_{name}", "OT", 1)
        series.equipment = Equipment("Acme", "Scanner", serial)
        inst = Instance(uid, sop_class, 1)
        inst.file_path = None
        inst.set_pixel_data(np.full(shape, fill, dtype=np.uint8))
        inst.set_attr("0008,0016", sop_class)
        inst.set_attr("0008,0030", "120000")
        inst.set_attr("0008,0060", "OT")
        inst.set_attr("0028,0004", "MONOCHROME2")
        series.instances.append(inst)
        study.series.append(series)
        patient.studies.append(study)
        session.store.patients.append(patient)

        session.save()
        session.close()

        session = DicomSession(str(db_file))
        opened.append(session)
        inst = next(i
                    for p in session.store.patients
                    for st in p.studies
                    for se in st.series
                    for i in se.instances
                    if i.sop_instance_uid == uid)

        arr = inst.get_pixel_data()
        assert arr is not None, "the reopened instance lost its pixels"
        assert arr.flags.writeable is False, (
            "this fixture exists to supply a read-only array; a writeable "
            "one makes every test built on it vacuous rather than red")
        # Drop the resident array so the redaction re-reads through the
        # loader rather than the copy this assertion just materialised.
        inst.unload_pixel_data()

        session.configuration.rules = [
            {"serial_number": serial, "redaction_zones": zones}]
        return session, inst

    yield make

    for session in opened:
        try:
            session.close()
        except Exception:  # pragma: no cover - teardown must not mask failures
            pass

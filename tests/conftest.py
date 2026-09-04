import collections
import faulthandler
import itertools
import multiprocessing
import os
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


def pytest_configure(config):
    """Take the fd and start the watchdog.

    Here rather than at import: global capture is suspended while this
    hook runs, so fd 2 is the real stderr. See the measurement above.
    """
    global _stderr_fd, _stderr_file
    if _stderr_fd is not None:  # pragma: no cover - one configure per run
        return
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


def pytest_collection(session):
    _mark("collection")


def pytest_runtest_logstart(nodeid, location):
    _mark("running test item", nodeid)


def pytest_sessionfinish(session, exitstatus):
    _mark("sessionfinish")


def pytest_unconfigure(config):
    _mark("unconfigure / atexit")


@pytest.fixture(autouse=True)
def redirect_logging(tmp_path):
    """Redirects isocenter.log to a temp file for all tests."""
    log_file = tmp_path / "isocenter.log"
    os.environ["ISOCENTER_LOG_FILE"] = str(log_file)
    yield
    if "ISOCENTER_LOG_FILE" in os.environ:
        del os.environ["ISOCENTER_LOG_FILE"]

@pytest.fixture
def dummy_pixel_array_2d():
    return np.zeros((512, 512), dtype=np.uint16)


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
    0008,0030 in Common', '[Type 2 Error] Missing 0018,0050 in CTImage',
    ...]`. Under SC Image Storage the CTImage module does not apply, which
    is why only the Common-module elements have to be supplied here.

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

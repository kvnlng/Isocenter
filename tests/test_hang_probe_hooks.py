"""The two conftest hooks the #250 hang probe depends on.

`.github/workflows/hang-probe.yml` loops the suite until an iteration
hangs, and it can only classify a hang if two things in
`tests/conftest.py` hold: a SIGUSR1 sent to the pytest parent dumps every
thread's stack (pytest's own faulthandler registers only the fatal
signals, so a TERM produces nothing -- the issue's retraction measured
that), and `ISOCENTER_HANG_PROBE_START_METHOD=fork` makes every pool the
suite builds fork rather than spawn, re-creating the population #260
removed. Both install inside `pytest_configure`, which has already run
for the session executing this file, so the two positive tests run a
nested pytest **in a subprocess** through `pytester` -- in-process
`runpytest` would reuse this session's configure and see nothing.

The conftest under test is this repository's own, copied into the
pytester directory verbatim, so a hook that moves or is renamed reddens
these rather than a probe run hours later.
"""
import multiprocessing
import os
import pathlib
import shutil
import sys

import pytest

pytest_plugins = ["pytester"]

REPO = pathlib.Path(__file__).resolve().parent.parent
CONFTEST = REPO / "tests" / "conftest.py"

#: The probe's only lever. Read by `tests/conftest.py` and never by the
#: package, which is why `docs/environment.md` has no row for it; see the
#: conftest comment for the argument.
PROBE_VARIABLE = "ISOCENTER_HANG_PROBE_START_METHOD"


def _copy_conftest(pytester):
    """This repository's conftest, and the `support` package it imports.

    `conftest.py` imports `support.root_guard` (#707); the package sits
    beside it on `sys.path` in the real tree and must in the copy too.
    """
    pytester.makeconftest(CONFTEST.read_text(encoding="utf-8"))
    shutil.copytree(CONFTEST.parent / "support", pytester.path / "support")


def _require_posix():
    """Skip on Windows: no fork start method and no SIGUSR1 there.

    A body-level skip call rather than a marker, because
    `tests/test_skip_contract.py`'s visitor recognises this form and the
    probe never runs on Windows anyway.
    """
    if sys.platform == "win32":
        pytest.skip("no fork start method and no SIGUSR1 on Windows")


def test_the_fork_override_is_inert_without_its_variable(monkeypatch):
    """The cry-wolf guard: an unset variable changes nothing.

    Green on both sides of the change, and a real test only because of
    the one below it: if the override ever installed unconditionally,
    every pool in every ordinary run would fork on Linux and the two
    spawn pins in `tests/test_parallel_contract.py` would go red with
    it -- this names the cause rather than leaving them to.

    The assertion is about *this* session, whose `pytest_configure` ran
    with the variable unset (or the test is skipped, above). The
    `delenv` states the premise; it cannot re-run configure.
    """
    if os.environ.get(PROBE_VARIABLE) == "fork":
        pytest.skip("the override is installed in this session by design; "
                    "the probe's fork arm is what this test guards against "
                    "happening anywhere else")
    monkeypatch.delenv(PROBE_VARIABLE, raising=False)

    assert multiprocessing.get_context("spawn").get_start_method() == "spawn", (
        "multiprocessing.get_context('spawn') no longer returns a spawn "
        "context with the probe variable unset; the fork override has "
        "escaped its guard and every ordinary run is now the fork "
        "population #260 removed")


def test_the_fork_override_reaches_every_pool_pin(pytester, monkeypatch):
    """With the variable set to `fork`, a request for spawn gets fork.

    All four pool pins (`parallel._run_on_recycling_pool`,
    `parallel._run_on_new_executor`, `Session._executor` and its OOM
    restart) are the literal `multiprocessing.get_context("spawn")`,
    read through the module attribute at construction time, so a
    rebound `get_context` reaches them all. Asserted twice in the inner
    run: on the module call directly, and on the `mp_context` handed to
    `ProcessPoolExecutor` by `run_parallel` under
    `ISOCENTER_FORCE_PROCESSES` -- the capture pattern
    `test_the_per_call_process_pool_pins_spawn` uses, so the two tests
    measure the same thing from opposite sides.
    """
    _require_posix()
    _copy_conftest(pytester)
    pytester.makepyfile(test_probe_fork="""
        import concurrent.futures
        import multiprocessing

        from isocenter import parallel


        def identity(x):
            return x


        def test_a_request_for_spawn_gets_fork(monkeypatch):
            assert multiprocessing.get_context("spawn").get_start_method() == "fork"

            captured = {}
            real = concurrent.futures.ProcessPoolExecutor

            class Recording(real):
                def __init__(self, *args, **kwargs):
                    captured.update(kwargs)
                    super().__init__(*args, **kwargs)

            monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", Recording)
            monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")

            result = list(parallel.run_parallel(
                identity, [1], show_progress=False, max_workers=1))
            assert result == [1]

            ctx = captured.get("mp_context")
            assert ctx is not None and ctx.get_start_method() == "fork", (
                f"run_parallel built its pool with mp_context={ctx!r} under "
                "the fork override; the override did not reach the pin")
    """)
    monkeypatch.setenv(PROBE_VARIABLE, "fork")

    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")

    result.assert_outcomes(passed=1)


def test_sigusr1_dumps_the_parents_threads(pytester, monkeypatch):
    """A SIGUSR1 to the pytest parent is a stack dump, not a death.

    Default disposition for SIGUSR1 is to terminate the process, silently.
    The probe sends it at the deadline precisely because pytest's
    faulthandler handles only SEGV/FPE/ABRT/BUS/ILL; `tests/conftest.py`
    registers USR1 on the fd the stall watchdog already duplicated while
    capture was suspended, which is the only channel measured to reach
    the run's stderr from inside a capturing pytest. The inner test
    signals itself and then sleeps so the handler runs before the test
    returns; the run must pass (the process survived) and stderr must
    carry faulthandler's `Current thread` header.
    """
    _require_posix()
    _copy_conftest(pytester)
    pytester.makepyfile(test_probe_usr1="""
        import os
        import signal
        import time


        def test_the_process_survives_its_own_usr1():
            os.kill(os.getpid(), signal.SIGUSR1)
            time.sleep(0.5)
    """)
    monkeypatch.delenv(PROBE_VARIABLE, raising=False)

    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")

    result.assert_outcomes(passed=1)
    assert "Current thread" in result.stderr.str(), (
        "SIGUSR1 did not produce a faulthandler dump on stderr; the probe's "
        "deadline signal would kill the run and name nothing (#250)")

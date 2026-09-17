"""The hang probe's loop script, run rather than read (#427).

`.github/workflows/hang-probe.yml` decides whether a release is blocked,
and until #427 nothing ran its loop script. `tests/test_packaging_contract.py`
reads the workflow's text: the triggers, the caps, a `set +e` regex. That
could not see what the script *does*. What it did was declare an
iteration `HANG` after `per_iteration_minutes` of wall-clock. Run
34488760203 killed a green suite that way, 0.5 s into interpreter
teardown after pytest had printed `1855 passed, 1 skipped in 899.50s`.

This file extracts the loop step's `run:` text from the YAML and
substitutes every `${{ ... }}`, failing if any survives. It runs the
script under `bash --noprofile --norc -eo pipefail`, which is exactly how
GitHub runs a `shell: bash` step, so the `-e` that `set +e` must switch
off is really live. A fake `python` stands in for pytest: first on PATH,
it plays one scenario per test. A Python `setsid` shim goes first on PATH
as well. macOS has no `setsid`, and `kill -KILL -- -$pid` needs pytest to
lead its own process group, so the shim is what both platforms run.

**The knobs.** The script reads four test-only variables,
`PROBE_UNIT_S` (seconds per input "minute"), `PROBE_POLL_S`,
`PROBE_GRACE_S` and `PROBE_MARGIN_S`, plus `PROBE_BUDGET_S`. All of them
are unset in CI, so each takes its production default. That lets the
scenarios run in seconds rather than tens of minutes without editing the
script's text. Editing the text would couple this file to exact
spellings such as `sleep 5`, and after a rename it would silently test
nothing. `test_packaging_contract.py` pins the defaults, and checks that
nothing in the workflow sets the knobs.

**What each scenario costs.** The whole file took 45.5-48.3 s on 3.12.14 and
46.4-48.7 s on 3.14.7t (macOS, ten runs each), all of it inside a full-suite run (the local gate, or the Run Tests step of `tests.yml` at release).
The script's clock is `date +%s`, which
has one-second resolution, so no scenario may depend on a sub-second
limit; every margin below is at least a whole second.

This imports no package module, so it needs no `TARGETS` row.
"""
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import time

import pytest
import yaml

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the loop is a bash script with POSIX process groups")

REPO = pathlib.Path(__file__).resolve().parents[1]
PROBE_WORKFLOW = REPO / ".github" / "workflows" / "hang-probe.yml"

#: GitHub's own flags for a `shell: bash` step.
_GITHUB_BASH = ["--noprofile", "--norc", "-eo", "pipefail"]

#: What the fake prints: pytest's `-v` line for a test that started, at
#: column 0, and a summary line of the shape pytest ends a run with.
_FAKE_PYTHON = r'''#!{python}
import os, signal, sys, time

d = os.environ["FAKE_DIR"]
scenario = os.environ["FAKE_SCENARIO"]
with open(os.path.join(d, "starts"), "a") as f:
    f.write("%d\n" % os.getpid())


def usr1(signum, frame):
    # What tests/conftest.py's SIGUSR1 registration does for real: every
    # thread's stack, into this log. The marker is how a test sees that
    # USR1 arrived before the KILL.
    print("FAKE-USR1-DUMP", flush=True)


signal.signal(signal.SIGUSR1, usr1)
# pytest's real first line. It is a column-0 run of `=` too, so a summary
# regex loosened to "a line of `=`" sees a finished run from the start.
print("============================= test session starts "
      "==============================", flush=True)


def started(i):
    print("tests/test_fake.py::test_%d PASSED" % i, flush=True)


def summary(words="20 passed"):
    print("=================== %s in 1.23s ===================" % words, flush=True)


if scenario == "clean":
    for i in range(20):
        started(i)
        time.sleep(0.02)
    summary()
    sys.exit(0)
if scenario == "failed":
    for i in range(3):
        started(i)
    print("FAILED tests/test_fake.py::test_1 - assert 1 == 2", flush=True)
    summary("1 failed, 2 passed")
    sys.exit(1)
if scenario == "locked":
    started(0)
    print("E   sqlite3.OperationalError: database is locked", flush=True)
    summary("1 failed")
    sys.exit(1)
if scenario == "silent":
    started(0)
    started(1)
    # A pool worker that joined pytest's process group: it must die with
    # the group KILL, not survive into the next iteration.
    child = os.fork()
    if child == 0:
        time.sleep(600)
        os._exit(0)
    with open(os.path.join(d, "grandchild"), "w") as f:
        f.write(str(child))
    # The conftest stall watchdog keeps writing through a real hang, and
    # names the stuck test indented (P3/P4): the log grows, the count
    # of tests started does not.
    while True:
        print("  last test item : tests/test_fake.py::test_1", flush=True)
        print('  File "/w/tests/test_fake.py", line 3 in test_1', flush=True)
        time.sleep(0.2)
if scenario == "no_exit":
    for i in range(20):
        started(i)
    summary()
    while True:
        time.sleep(1)
if scenario == "slow":
    i = 0
    while True:
        started(i)
        i += 1
        time.sleep(0.3)
if scenario == "late_summary":
    # A last test that runs long (under the stall deadline), then the
    # summary, then a teardown that is also under it, then exit 0.
    for i in range(3):
        started(i)
    time.sleep(3.3)
    summary("3 passed")
    time.sleep(3.3)
    sys.exit(0)
if scenario == "late_hang":
    # Starts a test every 0.3 s for PROGRESS_S, optionally prints the
    # summary, then goes silent for good: a hang that begins late in the
    # iteration, or in teardown after the summary.
    t_end = time.time() + float(os.environ["PROGRESS_S"])
    i = 0
    while time.time() < t_end:
        started(i)
        i += 1
        time.sleep(0.3)
    if os.environ.get("WITH_SUMMARY"):
        summary()
    while True:
        time.sleep(1)
if scenario == "sleep_then_clean":
    started(0)
    time.sleep(float(os.environ["FAKE_SECONDS"]))
    summary("1 passed")
    sys.exit(0)
raise SystemExit("unknown FAKE_SCENARIO %r" % scenario)
'''

#: `setsid(1)` as the loop uses it: become a session and group leader,
#: then exec the command in place, so `$!` is the group's id.
_SETSID_SHIM = r'''#!{python}
import os, sys
os.setsid()
os.execvp(sys.argv[1], sys.argv[1:])
'''


def _loop_step():
    workflow = yaml.safe_load(PROBE_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["probe"]["steps"]
    return next(s for s in steps if s.get("id") == "loop")


def _substituted(inputs):
    """The loop's `run:` text with every `${{ ... }}` replaced, or a failure."""
    values = {f"inputs.{k}": str(v) for k, v in inputs.items()}
    values["matrix.start_method"] = "spawn"
    text = _loop_step()["run"]

    def sub(match):
        body = match.group(1).strip()
        assert body in values, (
            f"the loop script uses `${{{{ {body} }}}}`, which this harness "
            f"does not supply; add it to the scenario inputs")
        return values[body]

    script = re.sub(r"\$\{\{(.*?)\}\}", sub, text)
    assert "${{" not in script, "an expression survived substitution"
    assert re.search(r"^\s*set \+e\b", script, re.M), (
        "the extracted loop text has no `set +e`; the harness is not "
        "running the script it thinks it is")
    return script


class _Run:
    def __init__(self, proc, tmp, summary, header=True):
        self.rc = proc.returncode
        self.out = proc.stdout + proc.stderr
        self.tmp = tmp
        self.summary = summary
        rows = [line for line in summary.splitlines() if line.startswith("| ")]
        if not header:
            assert not rows, f"rows were written:\n{summary}"
            self.rows, self.outcomes = [], []
            return
        assert rows and rows[0].startswith("| iter | outcome |"), (
            f"no summary header row was written:\n{summary}")
        self.rows = [[c.strip() for c in r.strip("|").split("|")]
                     for r in rows[1:]]
        self.outcomes = [r[1] for r in self.rows]

    @property
    def starts(self):
        path = self.tmp / "fake" / "starts"
        return len(path.read_text().split()) if path.exists() else 0

    def log(self, i=1):
        return (self.tmp / "probe-logs" / f"spawn-iter-{i}.log").read_text()


def _run_loop(tmp_path, scenario, *, iterations=1, per_iteration_minutes=30,
              stall_minutes=2, budget_s=None, extra_env=None, timeout=90,
              header=True):
    """Run the real loop script against the fake, in `tmp_path`."""
    fake = tmp_path / "fake"
    fakebin = tmp_path / "bin"
    fake.mkdir()
    fakebin.mkdir()
    for name, src in (("python", _FAKE_PYTHON), ("setsid", _SETSID_SHIM)):
        path = fakebin / name
        path.write_text(src.replace("{python}", sys.executable), encoding="utf-8")
        path.chmod(0o755)
    script = tmp_path / "loop.sh"
    script.write_text(_substituted({
        "iterations": iterations,
        "per_iteration_minutes": per_iteration_minutes,
        "stall_minutes": stall_minutes,
    }), encoding="utf-8")
    summary = tmp_path / "step_summary.md"
    summary.write_text("", encoding="utf-8")

    bash = shutil.which("bash")
    assert bash, "no bash on PATH"
    env = {k: v for k, v in os.environ.items() if not k.startswith("PROBE_")}
    env.update({
        "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}",
        # What GitHub supplies. `set -u` is live, so without these the
        # script dies on an unbound variable before its first row.
        "PROBE_SELECTION": "tests",
        "GITHUB_STEP_SUMMARY": str(summary),
        # One "minute" is a second; see the module docstring.
        "PROBE_UNIT_S": "1",
        "PROBE_POLL_S": "0.2",
        "PROBE_GRACE_S": "1",
        "PROBE_MARGIN_S": "0",
        "FAKE_DIR": str(fake),
        "FAKE_SCENARIO": scenario,
    })
    if budget_s is not None:
        env["PROBE_BUDGET_S"] = str(budget_s)
    env.update(extra_env or {})
    proc = subprocess.run([bash, *_GITHUB_BASH, str(script)], cwd=tmp_path,
                          env=env, capture_output=True, text=True,
                          timeout=timeout)
    return _Run(proc, tmp_path, summary.read_text(encoding="utf-8"), header)


@pytest.fixture(autouse=True)
def _reap(tmp_path):
    """Leave nothing behind, whatever the script did -- **after** the test.

    Every fake's process group, and the forked grandchild by pid, are
    killed at teardown. This must not run before a test's assertions: it
    was once in `_run_loop`'s `finally`, where it killed the grandchild
    itself, and a script that sent its KILL to the parent alone passed
    the group-kill check.
    """
    yield
    for starts in tmp_path.rglob("starts"):
        for pid in starts.read_text().split():
            try:
                os.killpg(int(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    for grandchild in tmp_path.rglob("grandchild"):
        try:
            os.kill(int(grandchild.read_text()), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, ValueError):
            pass


def _gone(pid, within=5.0):
    """True once `pid` no longer exists. A KILLed orphan is a zombie until
    init reaps it, and `kill(pid, 0)` succeeds on a zombie, so poll."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_a_progressing_run_is_clean(tmp_path):
    """The baseline: twenty tests, a summary, exit 0, a `clean` row."""
    run = _run_loop(tmp_path, "clean", iterations=2)
    assert run.starts == 2, run.out
    assert run.outcomes == ["clean", "clean"], run.summary
    assert run.rc == 0, run.out
    assert "::error::" not in run.out and "::warning::" not in run.out, run.out


def test_silence_is_a_hang_even_while_the_log_grows(tmp_path):
    """No new test started for `stall_minutes` is `HANG`, however much is written.

    The fake starts two tests and then writes the stall watchdog's lines
    every 0.2 s forever, as a real hang does (run 34073209412's fork log
    grew every 120 s for 850 s). **The count is anchored at column 0**:
    the watchdog names the stuck test indented, so an unanchored count
    sees progress every 0.2 s, never stalls, and reads the hang as `SLOW`.
    The ceiling here is 20 s against a 2 s stall, so that mistake shows as
    the wrong row, not as a pass.

    The kill sequence is checked here as well: USR1 lands before the KILL
    (the fake's dump marker is in the log), and the KILL goes to the whole
    group, because the fake's own forked child is gone afterwards.
    """
    run = _run_loop(tmp_path, "silent", iterations=3, per_iteration_minutes=20)
    assert run.outcomes == ["HANG"], run.summary
    assert "::error::HANG on iteration 1" in run.out, run.out
    assert run.rc == 1, run.out
    assert run.starts == 1, run.out
    assert "FAKE-USR1-DUMP" in run.log(1), run.log(1)[-2000:]
    grandchild = int((tmp_path / "fake" / "grandchild").read_text())
    assert _gone(grandchild), (
        "the fake's forked child outlived the kill: the KILL went to the "
        "parent alone, not to the process group")


def test_a_run_that_prints_its_summary_and_never_exits_is_a_hang_at_exit(tmp_path):
    """Silent after its summary is `HANG(exit)`, and it blocks (owner ruling, Q2).

    The summary line is part of the progress token, so this is told apart
    from a hang mid-suite. It still blocks: a process that will not exit
    is a hang to anyone who ran it. The real teardown in run 34488760203
    took 0.5 s, nowhere near a stall.
    """
    run = _run_loop(tmp_path, "no_exit", iterations=2)
    assert run.outcomes == ["HANG(exit)"], run.summary
    assert "::error::HANG(exit) on iteration 1" in run.out, run.out
    assert run.rc == 1, run.out


def test_the_silence_clock_restarts_at_the_summary(tmp_path):
    """A long last test, then the summary, then teardown, is `clean`.

    The summary line is part of the progress token, so the silence clock
    restarts when it is printed. Without it, the clock runs from the last
    test id, through the summary, and a teardown far shorter than the
    stall deadline is killed as `HANG(exit)`: 3.3 s of last test plus
    3.3 s of teardown reaches the 5 s deadline, though neither gap does.
    Each gap reads as at most 4 s on the script's whole-second `date`
    plus a 0.2 s poll, a whole second under 5; the pair reads as at
    least 6. (At 1.8 s gaps and a 3 s deadline the margin was 0.2 s.)
    """
    run = _run_loop(tmp_path, "late_summary", stall_minutes=5)
    assert run.outcomes == ["clean"], (run.summary, run.out)
    assert run.rc == 0, run.out


def test_a_run_still_progressing_at_the_ceiling_is_slow_not_a_hang(tmp_path):
    """#427 itself: running out of wall clock while tests still start is `SLOW`.

    `SLOW` is no verdict. It warns, stops the loop and exits 0, so a
    re-dispatch with a higher ceiling is the answer, not a blocked
    release. The fake starts a test every 0.3 s forever, so a test starts
    after the 3 s ceiling, which is what `SLOW` requires. The stall
    deadline is a minute, so only the ceiling can end it.
    """
    run = _run_loop(tmp_path, "slow", iterations=3, per_iteration_minutes=3,
                    stall_minutes=60)
    assert run.outcomes == ["SLOW"], run.summary
    assert "::warning::SLOW on iteration 1" in run.out, run.out
    assert "::error::" not in run.out, run.out
    assert run.rc == 0, run.out
    assert run.starts == 1, run.out
    assert "FAKE-USR1-DUMP" in run.log(1), "a SLOW run's dump says where the time went"


@pytest.mark.parametrize("with_summary, outcome", [
    (False, "HANG"),
    (True, "HANG(exit)"),
])
def test_a_hang_that_starts_near_the_ceiling_is_still_a_hang(
        tmp_path, with_summary, outcome):
    """Silence that begins less than `stall_minutes` before the ceiling is a hang.

    The PR #480 review's case, on the documented dispatch at
    `per_iteration_minutes=25`: run 34488760203's summary printed at
    899.5 s, and a teardown that then hung would have met the 1500 s
    ceiling after 600 s of silence and been called `SLOW` -- a warning,
    a green job, "not a hang". The ceiling fired on elapsed time even
    though nothing had started for most of it.

    `SLOW` now needs a test that **started** at or after the ceiling. A
    run that went silent before it falls through to the stall deadline,
    so an iteration ends by ceiling + stall at the latest. The review's
    scenario was a 12 s ceiling, a 4 s stall and 10 s of progress. This
    is the same shape at 5, 3 and 3.5 s: at the ceiling the fake has been
    silent at most 1.7 s real, which the whole-second clock reads as at
    most 2 s, under the 3 s stall, so the old script said `SLOW` every
    time. The last test starts at most 4 s (read) after the start, under
    the ceiling, so the new one never can.
    """
    extra = {"PROGRESS_S": "3.5"}
    if with_summary:
        extra["WITH_SUMMARY"] = "1"
    run = _run_loop(tmp_path, "late_hang", iterations=2, per_iteration_minutes=5,
                    stall_minutes=3, extra_env=extra)
    assert run.outcomes == [outcome], (run.summary, run.out)
    assert f"::error::{outcome} on iteration 1" in run.out, run.out
    assert "::warning::" not in run.out, run.out
    assert run.rc == 1, run.out
    # The bound the fall-through keeps: the ceiling plus the stall, plus
    # the kill's grace, to within the clock's second.
    assert int(run.rows[0][2]) <= 5 + 3 + 1 + 1, run.summary


@pytest.mark.parametrize("name, value", [
    ("per_iteration_minutes", "12.5"),  # `$(( 12.5 * 60 ))` kills bash mid-script
    ("stall_minutes", "0"),             # every poll would be a HANG
    ("stall_minutes", "08"),            # a leading 0 is octal in `$(( ))`
])
def test_a_minutes_input_that_is_not_a_positive_whole_number_is_refused(
        tmp_path, name, value):
    """The two minute inputs are checked before anything runs, with a message.

    GitHub's `type: number` accepts `12.5`. Before this check the script
    died on its first `$(( ))` with bash's own error, no `::error::` and
    no row, so the run read as an unexplained red.
    """
    inputs = {"per_iteration_minutes": 30, "stall_minutes": 2}
    inputs[name] = value
    run = _run_loop(tmp_path, "clean", header=False, **inputs)
    assert run.rc == 1, run.out
    assert f"::error::{name} must be a positive whole number of minutes" in run.out, run.out
    assert f"'{value}'" in run.out, run.out
    assert run.starts == 0, run.out


def test_an_iteration_that_cannot_fit_the_budget_is_not_started(tmp_path):
    """The loop stops itself before the step cap can (#243's shape).

    Iteration 1 sleeps 5.5 s, which the script's whole-second clock reads
    as 5 to 7 s. A 9 s budget lets it run (its limit is 9 - 1 s of grace
    = 8 s, above 7) and then leaves at most 9 - 5 = 4 s, below the
    longest iteration so far, so iteration 2 is never started. At 3.5 s
    and a 7 s budget no value satisfied both readings, and the test was a
    coin toss. The row says `BUDGET`, which is a
    warning and no verdict. The fake is silent for its 3.5 s, so the stall
    deadline is a minute here: at the harness's 2 s it would read as HANG.
    """
    run = _run_loop(tmp_path, "sleep_then_clean", iterations=3, budget_s=9,
                    stall_minutes=60, extra_env={"FAKE_SECONDS": "5.5"})
    assert run.outcomes == ["clean", "BUDGET"], run.summary
    assert run.starts == 1, run.out
    assert "::warning::BUDGET before iteration 2" in run.out, run.out
    assert "::error::" not in run.out, run.out
    assert run.rc == 0, run.out


def test_the_budget_ends_an_iteration_that_would_outrun_it(tmp_path):
    """Inside an iteration the limit is the budget's, when that is nearer.

    This is reviewer item 3's arithmetic. A 6 s budget against a 3 s grace
    caps the iteration at 3 s, well before its 30 s ceiling. The fake is
    silent for 30 s under a minute's stall deadline, so only the budget
    can end it, and it does although no test is starting: the
    budget-bound limit is hard, where the ceiling waits for the stall.
    And a budget that leaves no room for even the kill sequence starts
    nothing: the limit would be zero or negative, and `longest` is 0
    before the first iteration, so that check alone would let it through.
    """
    run = _run_loop(tmp_path, "sleep_then_clean", iterations=2, budget_s=6,
                    stall_minutes=60,
                    extra_env={"FAKE_SECONDS": "30", "PROBE_GRACE_S": "3"})
    assert run.outcomes == ["BUDGET"], run.summary
    assert run.starts == 1, run.out
    assert "::warning::BUDGET on iteration 1" in run.out, run.out
    assert run.rc == 0, run.out
    # The row's seconds include the kill's grace, and the whole
    # iteration, grace and all, has to fit the budget: 3 s of limit plus
    # 3 s of grace, within the clock's second. A limit that did not
    # reserve the grace would be 6 s and the row 9 s. The label alone
    # shows neither: with the ceiling left in place it still says BUDGET,
    # 30 s later.
    assert int(run.rows[0][2]) <= 6 + 1, run.summary

    (tmp_path / "second").mkdir()
    run = _run_loop(tmp_path / "second", "clean", iterations=2, budget_s=1)
    assert run.outcomes == ["BUDGET"], run.summary
    assert "not started" in run.rows[0][3], run.summary
    # Not `run.starts == 0` alone: a script that starts the fake and
    # kills it at once, at a limit of 0, can USR1 it before its Python
    # has written the start marker -- the default disposition kills it
    # first. The log exists iff the script ran the command at all, since
    # its redirect creates the file.
    assert not (tmp_path / "second" / "probe-logs" / "spawn-iter-1.log").exists(), run.out
    assert run.starts == 0, run.out
    assert run.rc == 0, run.out


@pytest.mark.parametrize("scenario, iterations, outcomes, rc", [
    ("locked", 3, ["LOCKED"], 1),
    ("failed", 2, ["failed(rc=1)", "failed(rc=1)"], 0),
])
def test_a_failing_run_is_classified_and_the_loop_goes_on(
        tmp_path, scenario, iterations, outcomes, rc):
    """Under GitHub's `-eo pipefail`, a non-zero pytest is a row, not an exit.

    Without `set +e`, the script exits at `wait "$pid"` on the first
    failing iteration, with no row and no `::error::`.
    `test_packaging_contract.py` checks for that line by regex; this
    checks that it works. `LOCKED` is occurrence 4/5's signature and
    blocks. A plain failure is recorded and the loop goes on.
    """
    run = _run_loop(tmp_path, scenario, iterations=iterations)
    assert run.outcomes == outcomes, (run.summary, run.out)
    assert run.rc == rc, run.out
    assert run.starts == len(outcomes), run.out
    if scenario == "locked":
        assert "::error::LOCKED on iteration 1" in run.out, run.out

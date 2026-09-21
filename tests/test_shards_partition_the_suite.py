"""`--shard=I/N` splits the suite into N jobs that together run it once (#707).

The failure this file exists to catch is green: a matrix that lists
three of four shards, or an assignment that drops a file, runs fewer
tests and reports success.

It reads tests/shard_timings.json, and says so by name, so that
`pytest --changed` selects this file (rule 7: the files that name a
changed path) when the timings are regenerated, instead of the suite.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from support import shards

REPO = Path(__file__).resolve().parent.parent


def test_parse_reads_index_and_count():
    assert shards.parse("2/4") == (2, 4)


@pytest.mark.parametrize("bad", ["0/4", "5/4", "2", "a/b", "2/0", "-1/4"])
def test_parse_refuses_a_shard_that_cannot_exist(bad):
    with pytest.raises(ValueError):
        shards.parse(bad)


def _assert_the_shards_partition_the_real_suite(n):
    files = shards.suite_files(REPO)
    assigned = shards.assign(files, shards.load_timings(REPO), n)
    assert len(assigned) == n
    flat = [f for shard in assigned for f in shard]
    assert sorted(flat) == sorted(files), "a file is missing or duplicated"
    assert len(flat) == len(set(flat)), "a file is in two shards"
    assert all(assigned), "a shard is empty"


def test_assignment_is_deterministic():
    # Equal weights, so every placement is a tie and only the tie-break
    # decides: with distinct weights the input order never mattered and
    # this passed without one (#707, mutant at implementation).
    files = [f"tests/test_{c}.py" for c in "abcdefgh"]
    timings = {f: 1.0 for f in files}
    assert shards.assign(files, timings, 3) == shards.assign(
        list(reversed(files)), dict(reversed(list(timings.items()))), 3)


def test_the_longest_files_are_spread_not_stacked():
    # Name order puts the long file last, where greedy-by-name would lay
    # it on a shard already holding one: 3 and 1. Longest first: 2 and 2.
    timings = {"tests/test_a.py": 1.0, "tests/test_b.py": 1.0,
               "tests/test_c.py": 2.0}
    assigned = shards.assign(timings, timings, 2)
    loads = sorted(sum(timings[f] for f in shard) for shard in assigned)
    assert loads == [2.0, 2.0]


def test_a_file_with_no_timing_gets_the_median_not_zero():
    timings = {"tests/test_a.py": 40.0, "tests/test_b.py": 10.0,
               "tests/test_c.py": 20.0}
    files = list(timings) + ["tests/test_x.py"]
    # Weighed at the median, 20: a | c, then x joins c (20 < 40) and b
    # joins a (40 = 40, lower index). Weighed at 0 or any small constant,
    # x goes last and lands with a instead.
    assert shards.assign(files, timings, 2) == [
        ["tests/test_a.py", "tests/test_b.py"],
        ["tests/test_c.py", "tests/test_x.py"]]


def test_the_recorder_sums_every_phase_per_file(tmp_path):
    recorder = shards.TimingRecorder()
    recorder.add("tests/test_a.py::test_x", 1.5)          # setup
    recorder.add("tests/test_a.py::test_x", 2.0)          # call
    recorder.add("tests/test_a.py::TestK::test_y[p0]", 0.5)
    recorder.add("tests/test_b.py::test_z", 4.0)
    out = tmp_path / "t.json"
    recorder.write(out)
    assert json.loads(out.read_text()) == {
        "tests/test_a.py": 4.0, "tests/test_b.py": 4.0}


def test_a_shard_run_collects_what_the_full_assignment_gives_it():
    """`--shard=I/N` on a few paths runs what shard I of the whole suite
    holds among them -- so a red CI shard reproduces locally by its
    number (#727 review). Disjoint-and-covering was not enough: assigning
    over the collected items, or running shard I+1's files, passed it.
    With today's timings shard 1/2 of this pair may be empty and exit 5;
    the comparison makes that the expected result, not an accident."""
    given = ["tests/test_crypto.py", "tests/test_shards_partition_the_suite.py"]
    expected = shards.assign(shards.suite_files(REPO),
                             shards.load_timings(REPO), 2)
    for index in (1, 2):
        out = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q",
             "-p", "no:cacheprovider", f"--shard={index}/2", *given],
            cwd=REPO, capture_output=True, text=True, timeout=300)
        assert out.returncode in (0, 5), out.stdout + out.stderr
        got = {line.split("::")[0] for line in out.stdout.splitlines()
               if "::" in line}
        assert got == set(given) & set(expected[index - 1]), (index, got)


def test_a_collected_file_outside_the_partition_is_refused(tmp_path):
    """A file no shard owns would be run by none of them, behind green.

    `suite_files` is a glob; pytest's collection is a different question
    with its own configuration. If they ever disagree, the shard run
    stops and names the file rather than deselecting it from all N.
    """
    proj = tmp_path / "proj"
    (proj / "tests").mkdir(parents=True)
    (proj / "extra").mkdir()
    shutil.copy(REPO / "pytest.ini", proj / "pytest.ini")
    shutil.copy(REPO / "tests" / "conftest.py", proj / "tests")
    shutil.copytree(REPO / "tests" / "support", proj / "tests" / "support")
    (proj / "tests" / "test_inside.py").write_text("def test_a():\n    pass\n")
    (proj / "extra" / "test_outside.py").write_text(
        "def test_b():\n    pass\n")
    # The copied conftest imports `isocenter`; name the tree under test
    # rather than relying on what this process inherited.
    env = dict(os.environ, PYTHONPATH=str(REPO), PYTHONDONTWRITEBYTECODE="1")
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "-p", "no:cacheprovider", "--shard=1/1", "tests", "extra"],
        cwd=proj, env=env, capture_output=True, text=True, timeout=300)
    assert out.returncode == 4, out.stdout + out.stderr
    assert "extra/test_outside.py" in out.stdout + out.stderr
    # Named with the rootdir it was measured against, so a `-c` or
    # `--rootdir` that moved it reads as that, not as a mystery.
    assert f"relative to rootdir {proj}" in out.stdout + out.stderr


def test_a_relative_timings_path_is_refused(tmp_path):
    """Relative, the recorder wrote into the repository root, and the root
    guard then blamed a test for it (#727 review). Refused rather than
    resolved: the documented form is absolute, and a resolved one would
    still land in the root whenever pytest was started there."""
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "-p", "no:cacheprovider", "--record-shard-timings=rt.json",
         "tests/test_crypto.py"],
        cwd=REPO, capture_output=True, text=True, timeout=300)
    assert out.returncode == 4, out.stdout + out.stderr
    assert "absolute" in out.stdout + out.stderr
    assert not (REPO / "rt.json").exists()


def _gate_workflow():
    import re
    import yaml
    workflow = yaml.safe_load(
        (REPO / ".github" / "workflows" / "tests.yml").read_text("utf-8"))
    job = workflow["jobs"]["test"]
    run = next(s for s in job["steps"] if s.get("id") == "suite")["run"]
    match = re.search(r"--shard=\$\{\{ matrix\.shard \}\}/(\d+)", run)
    assert match, f"the Run Tests step does not pass --shard: {run!r}"
    return job, int(match.group(1))


@pytest.mark.parametrize("extra", [0, 1, 3])
def test_the_shards_partition_the_real_suite(extra):
    """Spec §5: the N `tests.yml` divides by, and two others. Read from
    the workflow, so moving it to 6 moves this too (#727 review)."""
    _job, count = _gate_workflow()
    _assert_the_shards_partition_the_real_suite(count + extra)


def test_the_gate_workflow_runs_every_shard_it_divides_into():
    job, count = _gate_workflow()
    matrix = job["strategy"]["matrix"]
    listed = matrix["shard"]
    # An `exclude:` of {python-version: 3.14t, shard: 4} drops a quarter
    # of one version behind a list that still reads 1..N (#727 review).
    assert not {"include", "exclude"} & set(matrix), (
        "tests.yml's matrix has include/exclude: a combination it removes "
        "or adds is a shard this pin cannot see")
    assert listed == list(range(1, count + 1)), (
        f"tests.yml divides the suite into {count} shards and runs "
        f"{listed}: every shard not listed is a part of the suite "
        "that no job runs, behind a green check")
    # The summary line is what names a red shard in the release matrix's
    # table; one that omits the shard reports four lines per version
    # nobody can tell apart.
    summary = next(s for s in job["steps"]
                   if "GITHUB_STEP_SUMMARY" in s.get("run", ""))["run"]
    assert f"shard ${{{{ matrix.shard }}}}/{count}" in summary, summary

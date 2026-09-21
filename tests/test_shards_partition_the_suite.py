"""`--shard=I/N` splits the suite into N jobs that together run it once (#707).

The failure this file exists to catch is green: a matrix that lists
three of four shards, or an assignment that drops a file, runs fewer
tests and reports success.
"""
import json
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


@pytest.mark.parametrize("n", [2, 4, 7])
def test_the_shards_partition_the_real_suite(n):
    files = shards.suite_files(REPO)
    assigned = shards.assign(files, shards.load_timings(REPO), n)
    assert len(assigned) == n
    flat = [f for shard in assigned for f in shard]
    assert sorted(flat) == sorted(files), "a file is missing or duplicated"
    assert len(flat) == len(set(flat)), "a file is in two shards"
    assert all(assigned), "a shard is empty"


def test_assignment_is_deterministic():
    files = [f"tests/test_{c}.py" for c in "abcdefgh"]
    timings = {f: float(i) for i, f in enumerate(files)}
    assert shards.assign(files, timings, 3) == shards.assign(
        list(reversed(files)), dict(reversed(list(timings.items()))), 3)


def test_the_longest_files_are_spread_not_stacked():
    timings = {"tests/test_a.py": 100.0, "tests/test_b.py": 100.0,
               "tests/test_c.py": 1.0, "tests/test_d.py": 1.0}
    assigned = shards.assign(timings, timings, 2)
    loads = sorted(sum(timings[f] for f in shard) for shard in assigned)
    assert loads == [101.0, 101.0]


def test_a_file_with_no_timing_gets_the_median_not_zero():
    timings = {"tests/test_a.py": 10.0, "tests/test_b.py": 10.0,
               "tests/test_c.py": 10.0}
    files = list(timings) + ["tests/test_new.py"]
    assigned = shards.assign(files, timings, 2)
    # 4 files of equal weight over 2 shards: 2 and 2. With a zero
    # default the new file would ride along with two others: 3 and 1.
    assert sorted(len(s) for s in assigned) == [2, 2]


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


def test_a_shard_run_collects_only_its_own_files():
    seen = []
    for index in (1, 2):
        out = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q",
             "-p", "no:cacheprovider", f"--shard={index}/2",
             "tests/test_crypto.py",
             "tests/test_shards_partition_the_suite.py"],
            cwd=REPO, capture_output=True, text=True, timeout=300)
        assert out.returncode in (0, 5), out.stdout + out.stderr
        seen.append({line.split("::")[0] for line in out.stdout.splitlines()
                     if "::" in line})
    assert not (seen[0] & seen[1]), "a file was collected by both shards"
    assert seen[0] | seen[1] == {
        "tests/test_crypto.py", "tests/test_shards_partition_the_suite.py"}


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
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "-p", "no:cacheprovider", "--shard=1/1", "tests", "extra"],
        cwd=proj, capture_output=True, text=True, timeout=300)
    assert out.returncode == 4, out.stdout + out.stderr
    assert "extra/test_outside.py" in out.stdout + out.stderr


def test_the_gate_workflow_runs_every_shard_it_divides_into():
    import re
    import yaml
    workflow = yaml.safe_load(
        (REPO / ".github" / "workflows" / "tests.yml").read_text("utf-8"))
    job = workflow["jobs"]["test"]
    listed = job["strategy"]["matrix"]["shard"]
    run = next(s for s in job["steps"] if s.get("id") == "suite")["run"]
    match = re.search(r"--shard=\$\{\{ matrix\.shard \}\}/(\d+)", run)
    assert match, f"the Run Tests step does not pass --shard: {run!r}"
    count = int(match.group(1))
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

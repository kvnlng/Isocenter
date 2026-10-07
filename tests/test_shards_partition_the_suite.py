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
    # Started from tmp_path, so a regression writes rt.json there and
    # not into the repository root (#727 review).
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "-p", "no:cacheprovider", "--record-shard-timings=rt.json",
         str(REPO / "tests" / "test_crypto.py")],
        cwd=tmp_path, capture_output=True, text=True, timeout=300)
    assert out.returncode == 4, out.stdout + out.stderr
    assert "absolute" in out.stdout + out.stderr
    assert not (tmp_path / "rt.json").exists()
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


def _suite_step(job):
    return next(s for s in job["steps"] if s.get("id") == "suite")


def test_the_test_step_runs_for_every_matrix_entry():
    """A matrix that lists every shard still skips one when the step that
    runs the tests is conditional on the entry: `if: matrix.shard != 8` on
    `Run Tests` leaves the list reading 1..8, the job green, and an eighth
    of the suite unrun (review of #964: that mutant passed every workflow
    pin). So nothing that decides whether the tests run, or whether their
    failure counts, may read the matrix.

    Kills: an `if:` naming the matrix on the step or on the job;
    `continue-on-error` on either, which reports a red shard green."""
    job, _count = _gate_workflow()
    step = _suite_step(job)
    for where, holder in (("the Run Tests step", step), ("the test job", job)):
        condition = str(holder.get("if", ""))
        assert "matrix" not in condition, (
            f"{where} of tests.yml runs only `if: {condition}`: a matrix "
            "entry it skips is a shard no job runs, behind a green check")
        assert not holder.get("continue-on-error"), (
            f"{where} of tests.yml sets continue-on-error: a shard that "
            "fails would be reported as passing")
    # No `if:` at all today. One that does not read the matrix (a
    # repository guard, say) is not this pin's business, but the plain
    # shape is asserted too so that adding one is a decision somebody
    # reads this for.
    assert "if" not in step, step.get("if")


def _heaviest_shard_seconds(files, timings, count):
    """The recorded weight of the heaviest of `count` shards, weighing an
    untimed file as `shards.assign` does (the median of the timed ones)."""
    import statistics
    known = [timings[f] for f in files if f in timings]
    default = statistics.median(known) if known else 1.0
    return max(sum(float(timings.get(f, default)) for f in shard)
               for shard in shards.assign(files, timings, count))


def test_the_heaviest_shard_is_weighed_as_the_assignment_weighs_it():
    """Kills: the mean shard taken for the heaviest; an untimed file
    weighed at nothing."""
    timings = {"tests/test_a.py": 300.0, "tests/test_b.py": 100.0,
               "tests/test_c.py": 100.0}
    files = list(timings) + ["tests/test_new.py"]      # weighs the median, 100
    # a | b, c, new: 300 against 300. Then one more untimed file.
    assert _heaviest_shard_seconds(files, timings, 2) == 300.0
    assert _heaviest_shard_seconds(files + ["tests/test_newer.py"],
                                   timings, 2) == 400.0
    assert _heaviest_shard_seconds(files, timings, 1) == 600.0


#: The share of the `Run Tests` step a shard's recorded weight may take.
#: Half, because one runner was measured 1.75 times slower than another
#: at one commit (#935): 1.75 x 50% is 87% of the step.
_HEAVIEST_SHARD_SHARE = 0.5


def test_the_heaviest_shard_fits_the_step_with_room():
    """The suite grows, and four shards reached the 25-minute step with
    every test passing before anything said so (#935). This says so here,
    at the refresh of tests/shard_timings.json (RELEASING.md, "Cutting a
    release", step 8), instead of in a release run: when it is red, add
    shards to tests.yml or raise the step's cap and the family of timeouts
    with it, as that file's comments say.

    **What it rests on, and nothing checks:** the timings are runner
    seconds. A file recorded locally is about 2.3 times lighter per test
    file and passes this with the suite twice the size. And a test file
    with no timing weighs the median, so files added since the last
    refresh are under-counted.

    Kills: the shard count lowered or the cap lowered past what the suite
    needs; the share raised."""
    job, count = _gate_workflow()
    cap = _suite_step(job)["timeout-minutes"] * 60
    files = shards.suite_files(REPO)
    timings = shards.load_timings(REPO)
    # The file is the one being asked about: most of the suite is in it.
    assert len([f for f in files if f in timings]) > len(files) / 2
    heaviest = _heaviest_shard_seconds(files, timings, count)
    assert heaviest <= _HEAVIEST_SHARD_SHARE * cap, (
        f"the heaviest of tests.yml's {count} shards weighs {heaviest:.0f} s "
        f"by tests/shard_timings.json, over {_HEAVIEST_SHARD_SHARE:.0%} of "
        f"the {cap} s Run Tests step. A runner 1.75 times slower than the "
        "one recorded would run it at "
        f"{1.75 * heaviest / cap:.0%} of the cap. Add shards (the matrix "
        "list, the run line and the summary line move together) or raise "
        "the cap with the job cap and faulthandler_timeout.")
    # And the pin can speak: the same suite on one shard does not fit.
    assert _heaviest_shard_seconds(files, timings, 1) > cap


# --- refreshing tests/shard_timings.json from a release run (#935, Q3 A) --
#
# Every shard of tests.yml records its files' seconds and uploads them as
# the artifact `shard-timings-<version>-<shard>`. scripts/shard_timings.py
# merges one run's artifacts into the file the shards are cut from
# (RELEASING.md, "Cutting a release", step 8). Pure functions over literal
# dicts here: no run is read.

def _shard_timings():
    # By path and appended, as the selector's tests import test_map: a
    # scripts/<name>.py must not shadow a stdlib module in this process.
    scripts = str(REPO / "scripts")
    if scripts not in sys.path:
        sys.path.append(scripts)
    import shard_timings
    return shard_timings


A, B, C = "tests/test_a.py", "tests/test_b.py", "tests/test_c.py"


def _run(per_version):
    """{(version, shard): {file: seconds}} from {version: [shard dicts]}."""
    return {(version, index): dict(files)
            for version, parts in per_version.items()
            for index, files in enumerate(parts, start=1)}


def test_a_merge_takes_the_median_across_versions():
    """Kills: the mean or the maximum for the median (one slow runner
    would then set a file's weight); the versions summed; a file's
    seconds read from the first version only."""
    merge = _shard_timings().merge
    three = _run({"3.12": [{A: 1.0}, {B: 10.0}],
                  "3.13": [{A: 2.0}, {B: 30.0}],
                  "3.14t": [{A: 30.0}, {B: 20.0}]})
    assert merge(three, 2) == {A: 2.0, B: 20.0}
    # An even number of versions: the mean of the middle two, as the
    # file #935 seeded was made (four versions).
    four = {**three, **_run({"3.14": [{A: 4.0}, {B: 40.0}]})}
    assert merge(four, 2) == {A: 3.0, B: 25.0}
    # A version need not put a file in the shard another does: the
    # partition is the same for all, but the merge does not rest on it.
    moved = _run({"3.12": [{A: 1.0, B: 5.0}, {}], "3.13": [{A: 3.0}, {B: 7.0}]})
    assert merge(moved, 2) == {A: 2.0, B: 6.0}
    # A file one version reported no test of (its module skips at import
    # there) takes the median of the versions that ran it. That is not a
    # missing shard: every shard of every version is here.
    partly = _run({"3.12": [{A: 1.0, B: 9.0}], "3.13": [{A: 3.0}],
                   "3.14t": [{A: 5.0, B: 11.0}]})
    assert merge(partly, 1) == {A: 3.0, B: 10.0}
    # Sorted and rounded as the recorder writes, so a refresh is a diff
    # of numbers.
    out = merge(_run({"3.12": [{B: 1.004, A: 2.0}]}), 1)
    assert list(out) == [A, B] and out[B] == 1.0


@pytest.mark.parametrize("what, recordings, count, said", [
    ("a shard killed in one version",
     {("3.12", 1): {A: 1.0}, ("3.12", 2): {B: 1.0}, ("3.14t", 1): {A: 1.0}},
     2, "3.14t lacks shard 2"),
    ("the last shard missing from every version",
     {("3.12", 1): {A: 1.0}, ("3.14t", 1): {A: 1.0}}, 2, "lacks shard 2"),
    ("a shard the workflow does not have",
     {("3.12", 1): {A: 1.0}, ("3.12", 2): {B: 1.0}, ("3.12", 3): {C: 1.0}},
     2, "shard 3"),
    ("a file in two shards of one version",
     {("3.12", 1): {A: 1.0}, ("3.12", 2): {A: 1.0}}, 2, "tests/test_a.py"),
    ("nothing", {}, 2, "no recording"),
])
def test_a_merge_refuses_a_version_with_a_shard_missing(what, recordings,
                                                        count, said):
    """A killed shard uploads nothing, and a file merged from the versions
    that finished would weigh that shard's files at the median: the
    balance the refresh exists for, quietly gone. Kills: a partial set
    merged; the shard count inferred from what arrived."""
    with pytest.raises(ValueError) as refused:
        _shard_timings().merge(recordings, count)
    assert said in str(refused.value), (what, str(refused.value))


def test_a_runs_artifacts_are_read_by_their_names(tmp_path):
    """`gh run download` puts each artifact in a folder of its name.
    Kills: the version cut at its first dot or its `t` dropped; a folder
    that is not a recording read as one; an artifact with no file in it
    passed over."""
    tool = _shard_timings()
    for name, body in (("shard-timings-3.12-1", {A: 1.0}),
                       ("shard-timings-3.14t-1", {A: 3.0}),
                       ("shard-timings-3.14t-10", {B: 2.0})):
        (tmp_path / name).mkdir()
        (tmp_path / name / tool.ARTIFACT_FILE).write_text(json.dumps(body))
    (tmp_path / "dist").mkdir()                     # publish.yml's own artifact
    (tmp_path / "dist" / "x.whl").write_text("")
    assert tool.read_run(tmp_path) == {
        ("3.12", 1): {A: 1.0}, ("3.14t", 1): {A: 3.0}, ("3.14t", 10): {B: 2.0}}
    (tmp_path / "shard-timings-3.13-1").mkdir()
    with pytest.raises(ValueError) as refused:
        tool.read_run(tmp_path)
    assert "shard-timings-3.13-1" in str(refused.value)


def test_every_shard_uploads_the_recording_the_merge_reads():
    """The workflow's half, which no run on a PR exercises: the file the
    test step records is the file uploaded, under a name the reader
    parses back into this job's version and shard. Kills: the upload
    reading another path than the recording's; a name without the version
    (two versions' shards would overwrite each other) or without the
    shard; an upload skipped when a test fails; an upload that can fail
    the job."""
    tool = _shard_timings()
    job, _count = _gate_workflow()
    recorded = "${{ runner.temp }}/" + tool.ARTIFACT_FILE
    assert f"--record-shard-timings={recorded}" in _suite_step(job)["run"]
    uploads = [s for s in job["steps"]
               if str(s.get("uses", "")).startswith("actions/upload-artifact@")]
    assert len(uploads) == 1, uploads
    upload = uploads[0]
    assert upload["with"]["path"] == recorded
    name = upload["with"]["name"]
    assert name == ("shard-timings-${{ matrix.python-version }}"
                    "-${{ matrix.shard }}")
    # As the reader will meet it, for the version with the most in it.
    concrete = name.replace("${{ matrix.python-version }}", "3.14t").replace(
        "${{ matrix.shard }}", "7")
    assert tool._ARTIFACT.fullmatch(concrete).groups() == ("3.14t", "7")
    assert upload.get("if") == "always()"
    assert upload.get("continue-on-error") is True
    # After the tests, or there is nothing to upload.
    steps = job["steps"]
    assert steps.index(upload) > steps.index(_suite_step(job))


def test_the_merge_command_writes_the_file_the_shards_are_cut_from(tmp_path):
    """The command RELEASING.md step 8 gives, end to end, on a scratch run
    of two versions and the workflow's own shard count. Kills: the count
    not read from tests.yml; the file written in another shape than the
    recorder's; a refusal that still writes."""
    tool = _shard_timings()
    _job, count = _gate_workflow()
    assert tool.workflow_shard_count(REPO) == count
    run = tmp_path / "run"
    for version, scale in (("3.12", 1.0), ("3.14t", 3.0)):
        for index in range(1, count + 1):
            folder = run / f"shard-timings-{version}-{index}"
            folder.mkdir(parents=True)
            (folder / tool.ARTIFACT_FILE).write_text(json.dumps(
                {f"tests/test_{index}.py": scale * index}))
    out = tmp_path / "timings.json"
    env = {k: v for k, v in os.environ.items() if not k.startswith("COVERAGE_")}
    done = subprocess.run(
        [sys.executable, "-m", "scripts.shard_timings", "merge", str(run),
         "--out", str(out)],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    expected = {f"tests/test_{i}.py": 2.0 * i for i in range(1, count + 1)}
    assert json.loads(out.read_text()) == expected
    # Byte for byte what `--record-shard-timings` writes for those numbers.
    recorder = shards.TimingRecorder()
    for name, seconds in expected.items():
        recorder.add(name, seconds)
    recorder.write(tmp_path / "recorded.json")
    assert out.read_bytes() == (tmp_path / "recorded.json").read_bytes()
    assert f"2 versions, {count} shards" in done.stdout

    # One shard short: refused, and the file is left as it was.
    shutil.rmtree(run / f"shard-timings-3.14t-{count}")
    before = out.read_bytes()
    done = subprocess.run(
        [sys.executable, "-m", "scripts.shard_timings", "merge", str(run),
         "--out", str(out)],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode != 0
    assert f"3.14t lacks shard {count}" in done.stderr
    assert "Traceback" not in done.stderr
    assert out.read_bytes() == before

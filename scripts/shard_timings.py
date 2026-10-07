"""Refresh tests/shard_timings.json from a release run's artifacts (#935).

The CI suite runs as N shards per Python version, and which test files a
shard runs is a function of tests/shard_timings.json: one number of
seconds per file (tests/support/shards.py). Every shard of tests.yml
records what its files took (`--record-shard-timings`) and uploads that
as the artifact `shard-timings-<version>-<shard>`. This merges one run's
artifacts into the file, at the release's record-back (RELEASING.md,
"Cutting a release", step 8):

    gh run download <run id> --pattern 'shard-timings-*' --dir <dir>
    python -m scripts.shard_timings merge <dir> --out "$PWD/tests/shard_timings.json"

Per file, the **median across the versions**: one slow runner must not
set a file's weight, and a file is cut into one shard for every version.

**A set with a shard missing is refused, never merged.** A shard killed
at its step's timeout uploads nothing. Merged without it, that shard's
files would be absent from the file and weigh the median of the rest,
which is the imbalance the refresh exists to remove (#935: 78 untimed
files were 36% of the suite's seconds). Take the artifacts of a run in
which every shard finished, or leave the file as it is.

**The unit is a runner's seconds.** A local recording is about 2.3 times
lighter per file; never merge one with a run's, and never refresh the
file from a local run: the capacity pin in
tests/test_shards_partition_the_suite.py reads these numbers against the
workflow's step cap.

Hand-run release tooling: nothing here is imported by the suite's
conftest, and it imports nothing from tests/.
"""
import argparse
import json
import re
import statistics
import sys
from pathlib import Path

#: The file each artifact holds, as tests.yml names it.
ARTIFACT_FILE = "shard-timings.json"
#: `shard-timings-<version>-<shard>`: the version may hold dots and a
#: build letter (`3.14t`), so the shard is what follows the last hyphen.
_ARTIFACT = re.compile(r"shard-timings-(.+)-(\d+)")
_RUN_LINE = re.compile(r"--shard=\$\{\{ matrix\.shard \}\}/(\d+)")


def merge(recordings, count):
    """One run's recordings as the timings file's mapping.

    Args:
        recordings (dict): `{(version, shard): {test file: seconds}}`.
        count (int): The number of shards the workflow divides into.

    Returns:
        dict: `{test file: median seconds across the versions that
            recorded it}`, sorted by name and rounded as
            `TimingRecorder.write` rounds.

    Raises:
        ValueError: When nothing was recorded; a version lacks a shard of
            1..count or holds one outside it; or a file is in two shards
            of one version.
    """
    if not recordings:
        raise ValueError("no recording to merge")
    versions = sorted({version for version, _shard in recordings})
    problems = []
    per_version = {}
    for version in versions:
        held = {shard for v, shard in recordings if v == version}
        problems += [f"{version} lacks shard {shard} of {count}"
                     for shard in range(1, count + 1) if shard not in held]
        problems += [f"{version} holds shard {shard}, and the workflow "
                     f"divides into {count}"
                     for shard in sorted(held) if not 1 <= shard <= count]
        seconds = per_version[version] = {}
        for shard in sorted(held):
            for name, value in recordings[(version, shard)].items():
                if name in seconds:
                    problems.append(
                        f"{version} recorded {name} in two shards")
                seconds[name] = float(value)
    names = sorted(set().union(*per_version.values()))
    if problems:
        raise ValueError(
            "this is not one whole run, so nothing was merged: "
            + "; ".join(problems[:12])
            + (f"; and {len(problems) - 12} more" if len(problems) > 12 else ""))
    # Over the versions that recorded the file: one whose module skips at
    # import on a version (an optional dependency) reports no test there,
    # and that is not a missing shard.
    return {name: round(statistics.median(
        per_version[version][name] for version in versions
        if name in per_version[version]), 2)
        for name in names}


def read_run(directory):
    """The recordings under a `gh run download --dir` folder.

    Args:
        directory: A folder holding one folder per artifact.

    Returns:
        dict: `{(version, shard): {test file: seconds}}`. A folder that is
            not named as a shard's artifact is another artifact of the
            run (the built distributions) and is not read.

    Raises:
        ValueError: When a shard's artifact folder holds no recording.
    """
    recordings = {}
    for folder in sorted(Path(directory).iterdir()):
        named = _ARTIFACT.fullmatch(folder.name)
        if not named or not folder.is_dir():
            continue
        recorded = folder / ARTIFACT_FILE
        if not recorded.is_file():
            raise ValueError(
                f"{folder.name} holds no {ARTIFACT_FILE}; download the run "
                "again rather than merge without it")
        recordings[(named.group(1), int(named.group(2)))] = json.loads(
            recorded.read_text(encoding="utf-8"))
    return recordings


def workflow_shard_count(repo):
    """The N tests.yml's run line divides the suite by."""
    workflow = Path(repo) / ".github" / "workflows" / "tests.yml"
    found = _RUN_LINE.search(workflow.read_text(encoding="utf-8"))
    if not found:
        raise ValueError(f"{workflow} passes no --shard=<matrix shard>/N")
    return int(found.group(1))


def render(timings):
    """The file's text, as `TimingRecorder.write` writes it."""
    return json.dumps(dict(sorted(timings.items())), indent=1) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m scripts.shard_timings", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    merging = commands.add_parser(
        "merge", help="merge one run's shard-timings-* artifacts")
    merging.add_argument("directory", help="where `gh run download` put them")
    merging.add_argument("--out", required=True,
                         help="the file to write (tests/shard_timings.json)")
    args = parser.parse_args(argv)

    repo = Path(__file__).resolve().parent.parent
    try:
        count = workflow_shard_count(repo)
        recordings = read_run(args.directory)
        timings = merge(recordings, count)
    except ValueError as refused:
        # A sentence, not a traceback: this is run by hand at a release.
        print(f"shard_timings: {refused}", file=sys.stderr)
        return 1
    Path(args.out).write_text(render(timings), encoding="utf-8")
    versions = sorted({version for version, _shard in recordings})
    print(f"merged {len(versions)} versions, {count} shards "
          f"({', '.join(versions)}): {len(timings)} test files, "
          f"{sum(timings.values()):.0f} s in all -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

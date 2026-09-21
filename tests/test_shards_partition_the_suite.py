"""`--shard=I/N` splits the suite into N jobs that together run it once (#707).

The failure this file exists to catch is green: a matrix that lists
three of four shards, or an assignment that drops a file, runs fewer
tests and reports success.
"""
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

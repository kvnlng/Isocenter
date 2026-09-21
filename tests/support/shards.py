"""Which test files run in which CI shard (#707).

File granularity, so module-scoped fixtures stay together and the
partition can be checked without collecting. Pure functions: the
assignment is a deterministic function of (files, timings, n), which is
what lets a contract test assert the shards partition the suite.
"""
import json
import statistics
from pathlib import Path

TIMINGS_FILE = "tests/shard_timings.json"


def parse(spec):
    try:
        index, count = (int(part) for part in spec.split("/"))
    except ValueError:
        raise ValueError(f"--shard wants I/N, e.g. 2/4; got {spec!r}") from None
    if count < 1 or not 1 <= index <= count:
        raise ValueError(f"--shard={spec}: I must be within 1..N and N >= 1")
    return index, count


def suite_files(repo):
    """Every file the suite is made of, as `tests/test_x.py`.

    The flat glob is the whole suite (spec §2 ruling 1): `pytest.ini`
    names no `python_files`, so pytest collects `test_*.py`, and nothing
    under `tests/` is nested. `conftest.py` refuses a collected file this
    does not list rather than dropping it from every shard.
    """
    return sorted(path.relative_to(repo).as_posix()
                  for path in (Path(repo) / "tests").glob("test_*.py"))


def load_timings(repo):
    path = Path(repo) / TIMINGS_FILE
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def assign(files, timings, n):
    """Longest-processing-time greedy; ties broken by name, then index.

    A file with no timing weighs the median of those that have one, so a
    new test file is balanced on the day it is added rather than piled
    onto whichever shard is lightest at weight zero.
    """
    files = sorted(set(files))
    known = [timings[f] for f in files if f in timings]
    default = statistics.median(known) if known else 1.0
    weight = {f: float(timings.get(f, default)) for f in files}
    result = [[] for _ in range(n)]
    loads = [0.0] * n
    for name in sorted(files, key=lambda f: (-weight[f], f)):
        lightest = min(range(n), key=lambda k: (loads[k], k))
        result[lightest].append(name)
        loads[lightest] += weight[name]
    return [sorted(shard) for shard in result]

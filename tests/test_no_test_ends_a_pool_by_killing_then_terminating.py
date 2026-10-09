"""No test ends a pool by killing its workers and then terminating it (#1010).

`test_the_pool_internals_the_recycling_watch_reads_are_there`
(`tests/test_parallel_contract.py`) ended its one-worker recycling pool in

    finally:
        for process in started + list(pool._pool):
            try:
                process.kill()
            except (OSError, ValueError):
                pass
        pool.terminate()

which is both things the comment in `parallel._end_recycling_pool` forbids.
It kills while the pool is RUN, and it calls `terminate()` on the caller's
thread. `terminate()` begins in `Pool._help_stuff_finish` by acquiring the
inqueue's read lock, which an idle worker holds while it waits in `get()`.
A worker killed there leaves the lock held for good, `_terminate_pool` has
already stopped the worker handler, so no replacement is started, and the
test's main thread waits on a lock nobody will release, with no child left:
the stack reported once under load on 3.14t. With the worker given time to
reach that read before the kill, it hung 3 of 3 on 3.12 and on 3.14t.

A test that ends its own pool cannot assert its own ending, since the
ending runs after its last assertion. So the shape is refused here, in the
syntax tree of every Python file under `tests/`.

## Why every file, and why this file

The one ending of that shape was in `tests/test_parallel_contract.py`, and
that is the only file under `tests/` that calls `terminate()` on a pool at
all. The walk still reads every file, because the next test of a pool's
internals may be written anywhere, and its author will copy an ending from
a neighbour.

It lives in a file of its own because of what `pytest --changed` reads. A
walk of `*.py` is selected by every changed Python file, and a file that
parses source is selected with every module it names. Inside
`tests/test_parallel_contract.py` either would bring that whole file, which
spawns pools for minutes, into nearly every selection. Here the cost is one
parse of the tree. A change to that file selects this one by name.

## What this deliberately cannot see

- **The receiver's type.** A syntax tree does not say what `.kill()` and
  `.terminate()` are called on, so the two attribute names in one `finally`
  are refused whatever they are called on. A `subprocess.Popen` ended by
  `terminate()`, a wait and then `kill()` in one `finally` would be refused
  too. None exists under `tests/`; one that is written ends the process in
  a helper, or this file learns to tell them apart then.
- **An ending split across blocks.** A `kill()` in the `try` and a
  `terminate()` in its `finally`, or the two in a helper function called
  from the `finally`, are not seen. A text search would miss the second as
  well; the first is the same hazard and is not this shape.
- **`terminate()` alone.** `test_the_pool_internals_the_recycling_exit_reads_are_there`
  ends in `finally: pool.terminate()`, on the caller's thread and under RUN
  when an assertion above it fails. It kills nothing, so no lock is left
  held, and it is allowed.
- **`os.kill(pid, …)`.** The call is `kill` on `os`, and so it is seen, by
  the same name. `signal` numbers are not read.
"""
import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"

THE_FILE_IT_WAS_FOUND_IN = "tests/test_parallel_contract.py"

# The ending as it stood on `main` until #1010, kept as text so the
# detector is run against the thing it was written for on every run, and
# not only on the day the ending was replaced.
THE_ENDING_REFUSED = '''
def test_a_pool():
    pool = make()
    started = []
    try:
        started = list(pool._started)
    finally:
        for process in started + list(pool._pool):
            try:
                process.kill()
            except (OSError, ValueError):
                pass
        pool.terminate()
'''

# The neighbour's ending: `terminate()` with nothing killed before it.
TERMINATE_ALONE = '''
def test_a_pool():
    pool = make()
    try:
        pool.close()
        pool.join()
        pool.terminate()
    finally:
        pool.terminate()
'''

# The `recorded_pools` fixture's ending: workers killed once the pool that
# owned them has been ended by the library, and no `terminate()`.
KILL_ALONE = '''
def recorded():
    started = []
    try:
        yield started
    finally:
        for process in list(started):
            try:
                process.kill()
            except (OSError, ValueError, AttributeError):
                pass
'''

# The ending #1010 put in its place.
THROUGH_THE_LIBRARY = '''
def test_a_pool():
    pool = make()
    try:
        list(pool.imap_unordered(identity, range(4)))
    finally:
        parallel._end_recycling_pool(pool, finished=False)
'''

_TRIES = (ast.Try,) + ((ast.TryStar,) if hasattr(ast, "TryStar") else ())


def _method_calls(statements):
    """The attribute names called anywhere inside `statements`."""
    names = set()
    for statement in statements:
        for node in ast.walk(statement):
            if isinstance(node, ast.Call) and isinstance(node.func,
                                                         ast.Attribute):
                names.add(node.func.attr)
    return names


def _finally_blocks(source, filename="<source>"):
    """Every `finally` in `source`, as (line of its `try`, names called)."""
    return [(node.lineno, _method_calls(node.finalbody))
            for node in ast.walk(ast.parse(source, filename=filename))
            if isinstance(node, _TRIES) and node.finalbody]


def _kills_then_terminates(source, filename="<source>"):
    """Lines of each `try` whose `finally` calls both `.kill()` and
    `.terminate()`."""
    return [line for line, names in _finally_blocks(source, filename)
            if {"kill", "terminate"} <= names]


def _python_files():
    return sorted(TESTS.rglob("*.py"))


def test_no_test_ends_a_pool_by_killing_then_terminating():
    """No `finally` under `tests/` calls both `.kill()` and `.terminate()`.

    End a recycling pool with `parallel._end_recycling_pool(pool,
    finished=False)`: it runs the stdlib's exit on a helper thread, kills by
    sentinel only what is still running after the grace, and holds the
    caller no longer than its two bounds.

    Killing mutation: the old ending restored in
    `test_the_pool_internals_the_recycling_watch_reads_are_there`.
    """
    offenders = []
    for path in _python_files():
        name = path.relative_to(ROOT).as_posix()
        for line in _kills_then_terminates(
                path.read_text(encoding="utf-8"), name):
            offenders.append(f"{name}:{line}")
    assert not offenders, (
        "a `finally` kills workers and then calls terminate() on the "
        "caller's thread; a worker killed while it waits for a task holds "
        "the pool's inqueue lock for good, and terminate() waits on it "
        "(#1010). End the pool with parallel._end_recycling_pool(pool, "
        "finished=False) instead. The `try` is at: " + ", ".join(offenders))


def test_the_ending_that_hung_is_the_one_refused():
    """The detector, against the ending it was written for and the three
    beside it that are allowed.

    Killing mutations: the detector reading `node.body` for `finalbody`
    (the first then passes); either name dropped from the pair (the
    neighbour's ending, or the fixture's, is then refused).
    """
    assert _kills_then_terminates(THE_ENDING_REFUSED) == [5]
    assert _kills_then_terminates(TERMINATE_ALONE) == []
    assert _kills_then_terminates(KILL_ALONE) == []
    assert _kills_then_terminates(THROUGH_THE_LIBRARY) == []


def test_the_walk_reads_the_file_the_ending_was_found_in():
    """The walk is not vacuous: it reaches the file #1010 was found in, and
    sees there a `finally` that kills, one that terminates, and the
    library's exit.

    A walk that had lost that file (moved, renamed, or a pattern that no
    longer matches) would pass for good with nothing read.
    """
    path = ROOT / THE_FILE_IT_WAS_FOUND_IN
    assert path in _python_files(), path
    blocks = _finally_blocks(path.read_text(encoding="utf-8"), path.name)
    assert any("kill" in names for _, names in blocks), blocks
    assert any("terminate" in names for _, names in blocks), blocks
    assert any("_end_recycling_pool" in names for _, names in blocks), blocks

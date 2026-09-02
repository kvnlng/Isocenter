"""`mutation_probe` must not score a mutant it never ran.

The probe rewrites a module, runs pytest in a subprocess, and reads the
exit code as a verdict about the mutation. That chain has one silent
break in it. CPython validates a timestamp-based `.pyc` against the
source's `(mtime, size)` pair with the mtime truncated to whole seconds,
and the probe writes `ast.unparse` output -- so two consecutive mutants
of the same shape are frequently byte-identical in length. When a test
run is quick enough for both writes to land in the same second, the
interpreter reuses the previous mutant's bytecode and pytest never sees
the mutation being scored. The verdict is then about code that was not
there: a phantom `SURVIVED` that reads exactly like a real coverage gap,
or a phantom kill that hides one (#174).

Three things are pinned here. `run()` must disable bytecode writing in
the subprocess, which is what stops each run planting the trap for the
next one. `assert_fresh()` must abort on a cache entry that would be
reused and *not* abort on one that would not -- a guard that fired on
the size half alone would fire on nearly every sample of every run and
the tool would be unusable. And `main()` must actually call it, before
every run including the control: these were written after mutation
testing the fix showed that deleting the `assert_fresh()` calls from
`main()` altogether left the suite green, which would have made the
guard the decoration this entry says it must not be.
"""
import importlib.util
import os
import pathlib
import py_compile
import struct
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import mutation_probe  # noqa: E402


def _compiled(tmp_path, text):
    """A source file plus a fresh timestamp-validated `.pyc` for it."""
    src = tmp_path / "victim.py"
    src.write_text(text, encoding="utf-8")
    py_compile.compile(
        str(src), doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
    return src


def _recorded_mtime(src):
    cache = pathlib.Path(importlib.util.cache_from_source(str(src)))
    return struct.unpack("<I", cache.read_bytes()[8:12])[0]


def _parent_cache(src):
    """This process's own cache path for `src` -- the tests below that pass
    it are deliberately fabricating the parent interpreter's entry, and
    saying so at the call site is the point of the explicit parameter."""
    return pathlib.Path(importlib.util.cache_from_source(str(src)))


def _stub_interpreter(tmp_path, body):
    """An executable that stands in for `PYTEST[0]`."""
    stub = tmp_path / "fake-python"
    stub.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    stub.chmod(0o755)
    return stub


def test_the_cache_path_is_asked_of_the_interpreter_that_runs_the_tests(
        tmp_path, monkeypatch):
    """`cache_from_source` in this process is a guess about another one.

    The parent's `sys.implementation.cache_tag` names a file the pytest
    subprocess never touches whenever the two interpreters differ --
    pyenv shim beside `.venv` is the routine case, not the exotic one
    (#201). So the path has to come out of `PYTEST[0]`'s own mouth: this
    stub answers something no local `cache_from_source` call could ever
    produce, and only asking the subprocess can return it.
    """
    log = tmp_path / "calls.log"
    stub = _stub_interpreter(
        tmp_path,
        f'echo "$3" >> "{log}"\necho "/sentinel$3.faketag-000.pyc"\n')
    monkeypatch.setattr(mutation_probe, "PYTEST",
                        [str(stub)] + mutation_probe.PYTEST[1:])
    mutation_probe._SUBPROCESS_CACHE_PATHS.clear()

    a = tmp_path / "victim_a.py"
    b = tmp_path / "victim_b.py"
    a.write_text("A = 1\n", encoding="utf-8")
    b.write_text("B = 1\n", encoding="utf-8")

    got_a = mutation_probe.subprocess_cache_path(a)
    got_b = mutation_probe.subprocess_cache_path(b)

    assert got_a == pathlib.Path(f"/sentinel{a.resolve()}.faketag-000.pyc")
    assert got_b == pathlib.Path(f"/sentinel{b.resolve()}.faketag-000.pyc")
    assert got_a != got_b, "memoization must be per source path, not global"

    # Memoized per path: a second ask for the same file must not spawn.
    assert mutation_probe.subprocess_cache_path(a) == got_a
    spawned = log.read_text(encoding="utf-8").splitlines()
    assert spawned.count(str(a.resolve())) == 1, spawned


def test_assert_fresh_inspects_the_cache_the_test_interpreter_would_read(
        tmp_path):
    """The guard must open the file it was told about, not the parent's.

    Deterministic #174 collision (same bytes, mtime frozen to the
    recorded second) built under the parent's tag, then relocated to a
    fabricated tag standing in for the subprocess's. Old behaviour
    derived the parent-tag path locally, found nothing there, and
    returned -- a silent no-op in exactly the stale case it exists for.
    """
    src = _compiled(tmp_path, "A = 1\n")
    parent = _parent_cache(src)
    fabricated = parent.with_name("victim.faketag-000.pyc")
    fabricated.write_bytes(parent.read_bytes())
    parent.unlink()
    src.write_text("A = 2\n", encoding="utf-8")  # same size
    recorded = struct.unpack("<I", fabricated.read_bytes()[8:12])[0]
    os.utime(src, (recorded, recorded))

    with pytest.raises(SystemExit) as exc:
        mutation_probe.assert_fresh(src, cache=fabricated)
    assert "stale" in str(exc.value).lower()

    # And the converse: a colliding parent-tag entry back on disk must
    # not fire the guard when the subprocess's own path holds nothing --
    # the parent's file is not the one pytest would reuse.
    parent.write_bytes(fabricated.read_bytes())
    fabricated.unlink()
    mutation_probe.assert_fresh(src, cache=fabricated)


def test_a_missing_test_interpreter_aborts_before_any_verdict(
        tmp_path, monkeypatch):
    """A probe that cannot ask its interpreter must refuse, by name.

    Today that failure is a raw FileNotFoundError out of the first
    subprocess call; a worktree without a `.venv` hits it every time.
    The probe prefers aborting over reporting, so the refusal is a
    SystemExit that says which interpreter is missing.
    """
    missing = str(tmp_path / "no-such-venv" / "bin" / "python")
    monkeypatch.setattr(mutation_probe, "PYTEST",
                        [missing] + mutation_probe.PYTEST[1:])
    mutation_probe._SUBPROCESS_CACHE_PATHS.clear()
    src = tmp_path / "victim.py"
    src.write_text("A = 1\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        mutation_probe.subprocess_cache_path(src)
    assert missing in str(exc.value)


def test_a_failing_cache_query_aborts_rather_than_guessing(
        tmp_path, monkeypatch):
    """Non-zero exit or empty output is an abort, not a `Path("")`.

    An interpreter that exists but cannot answer -- wrong binary, broken
    venv -- must not degrade into a guard pointed at an empty path,
    which would be #201's silent no-op wearing a different hat.
    """
    for body in ("exit 3\n", "exit 0\n"):
        stub = _stub_interpreter(tmp_path, body)
        monkeypatch.setattr(mutation_probe, "PYTEST",
                        [str(stub)] + mutation_probe.PYTEST[1:])
        mutation_probe._SUBPROCESS_CACHE_PATHS.clear()
        src = tmp_path / "victim.py"
        src.write_text("A = 1\n", encoding="utf-8")

        with pytest.raises(SystemExit) as exc:
            mutation_probe.subprocess_cache_path(src)
        assert str(stub) in str(exc.value)


def test_assert_fresh_allows_a_write_the_cache_cannot_match(tmp_path):
    src = _compiled(tmp_path, "A = 1\n")
    src.write_text("A = 22222222\n", encoding="utf-8")  # size differs
    mutation_probe.assert_fresh(src, cache=_parent_cache(src))


def test_assert_fresh_aborts_when_a_stale_pyc_would_validate(tmp_path):
    """The #174 collision, made deterministic: same bytes, same second."""
    src = _compiled(tmp_path, "A = 1\n")
    src.write_text("A = 2\n", encoding="utf-8")
    recorded = _recorded_mtime(src)
    os.utime(src, (recorded, recorded))

    with pytest.raises(SystemExit) as exc:
        mutation_probe.assert_fresh(src, cache=_parent_cache(src))
    assert "stale" in str(exc.value).lower()


def test_run_disables_bytecode_writing_without_clearing_the_environment(monkeypatch):
    """`-B` would not reach `run_parallel()`'s spawned workers; the variable does.

    And the environment has to be inherited rather than replaced: a bare
    `env=` dict drops PATH, and that failure would be scored as a kill.
    """
    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(mutation_probe.subprocess, "run", fake_run)
    assert mutation_probe.run(["tests/never_actually_runs.py"]) is True
    assert seen["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
    assert seen["env"]["PATH"] == os.environ["PATH"]


def test_assert_fresh_allows_a_same_second_write_of_a_different_size(tmp_path):
    """Half the key is not the key. Pinned with the clock, not against it.

    Without `os.utime` this case is decided by whether the rewrite happened
    to land in the same whole second as the compile, so it pins the mtime
    half on some runs and the size half on others. Freezing the mtime to
    the recorded value makes it always the size half -- which is the half
    that has to be able to say "reuse this".
    """
    src = _compiled(tmp_path, "A = 1\n")
    src.write_text("A = 22222222\n", encoding="utf-8")
    recorded = _recorded_mtime(src)
    os.utime(src, (recorded, recorded))

    mutation_probe.assert_fresh(src, cache=_parent_cache(src))


def test_assert_fresh_allows_a_pycache_left_behind_by_an_earlier_run(tmp_path):
    """The size half collides constantly; only the mtime half saves this one.

    A `__pycache__` already on disk when the probe starts records an mtime
    in the past, and the probe's writes are always later -- so it can match
    on size and still never validate. Observed in a real post-fix probe run,
    where a frozen header `(1787998703, 11905)` sat next to a mutant of
    exactly 11905 bytes. A guard that aborted here would fire on every
    sample of every run and the tool would be unusable (#174).
    """
    src = _compiled(tmp_path, "A = 1\n")
    src.write_text("A = 2\n", encoding="utf-8")  # same size as the compiled source
    recorded = _recorded_mtime(src)
    os.utime(src, (recorded + 5, recorded + 5))

    mutation_probe.assert_fresh(src, cache=_parent_cache(src))


def test_assert_fresh_truncates_the_second_rather_than_rounding_it(tmp_path):
    """CPython compares `int(st['mtime'])`, not `round(...)`.

    `_bootstrap_external.SourceLoader.get_code` truncates; a source written
    0.6s into the recorded second still validates against the cache and the
    stale bytecode is reused. Rounding would call that fresh and let the
    phantom verdict through in exactly the sub-second regime where the
    collision actually fires.
    """
    src = _compiled(tmp_path, "A = 1\n")
    src.write_text("A = 2\n", encoding="utf-8")
    recorded = _recorded_mtime(src)
    os.utime(src, (recorded + 0.6, recorded + 0.6))
    assert int(src.stat().st_mtime) == recorded != round(src.stat().st_mtime)

    with pytest.raises(SystemExit):
        mutation_probe.assert_fresh(src, cache=_parent_cache(src))


def test_assert_fresh_aborts_on_an_unchecked_hash_pyc(tmp_path):
    """PEP 552's two hash modes are not one case.

    CHECKED_HASH is verified against the source's own hash on every import
    and cannot go stale. UNCHECKED_HASH is reused without looking at the
    source at all -- worse than the timestamp case, not better, because no
    write the probe makes can ever invalidate it.
    """
    src = tmp_path / "victim.py"
    src.write_text("A = 1\n", encoding="utf-8")
    py_compile.compile(
        str(src), doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
    src.write_text("A = 2\n", encoding="utf-8")

    with pytest.raises(SystemExit):
        mutation_probe.assert_fresh(src, cache=_parent_cache(src))

    src.write_text("A = 1\n", encoding="utf-8")
    py_compile.compile(
        str(src), doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH)
    src.write_text("A = 2\n", encoding="utf-8")
    mutation_probe.assert_fresh(src, cache=_parent_cache(src))


def test_main_guards_the_bytes_of_every_run_including_the_control(tmp_path, monkeypatch):
    """A guard nothing calls is decoration.

    `assert_fresh()` is only worth having if it runs immediately before
    every `run()` -- the control included, since a control scored against
    stale bytecode invalidates the samples under it too -- if what it
    inspected is the file the tests were then shown, and if the cache it
    was pointed at came from `subprocess_cache_path` rather than a local
    derivation: a helper `main()` never calls is #201 still open (the
    wrong-tag path, just with better tooling beside it).
    """
    (tmp_path / "victim.py").write_text(
        "def f(x):\n    return x == 1 and not x\n", encoding="utf-8")
    calls = []
    sentinel = pathlib.Path("/sentinel/victim.faketag-000.pyc")

    monkeypatch.setattr(mutation_probe, "REPO", tmp_path)
    monkeypatch.setattr(mutation_probe, "TARGETS", {"victim.py": (["t.py"], 30)})
    monkeypatch.setattr(mutation_probe, "subprocess_cache_path",
                        lambda p: sentinel)
    monkeypatch.setattr(mutation_probe, "assert_fresh",
                        lambda p, c: calls.append(("guard", p.read_text(), c)))
    monkeypatch.setattr(mutation_probe, "run",
                        lambda t: calls.append(("run", (tmp_path / "victim.py").read_text(), None)) or True)
    monkeypatch.setattr(sys, "argv", ["mutation_probe"])

    mutation_probe.main()

    assert len(calls) >= 4, calls          # control + at least one sample, guarded
    assert [k for k, _, _ in calls] == ["guard", "run"] * (len(calls) // 2), calls
    for (_, guarded, cache), (_, scored, _) in zip(calls[::2], calls[1::2]):
        assert guarded == scored
        assert cache == sentinel, "the guard was not handed the subprocess's cache path"

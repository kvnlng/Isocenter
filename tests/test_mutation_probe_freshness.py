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

Two halves are pinned here. `run()` must disable bytecode writing in the
subprocess, which is what stops each run planting the trap for the next
one; and `assert_fresh()` must abort rather than return a verdict if a
cached `.pyc` would be reused anyway.
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


def test_assert_fresh_allows_a_write_the_cache_cannot_match(tmp_path):
    src = _compiled(tmp_path, "A = 1\n")
    src.write_text("A = 22222222\n", encoding="utf-8")  # size differs
    mutation_probe.assert_fresh(src)


def test_assert_fresh_aborts_when_a_stale_pyc_would_validate(tmp_path):
    """The #174 collision, made deterministic: same bytes, same second."""
    src = _compiled(tmp_path, "A = 1\n")
    src.write_text("A = 2\n", encoding="utf-8")
    recorded = _recorded_mtime(src)
    os.utime(src, (recorded, recorded))

    with pytest.raises(SystemExit) as exc:
        mutation_probe.assert_fresh(src)
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

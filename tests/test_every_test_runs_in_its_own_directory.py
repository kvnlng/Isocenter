"""Every test runs in its own directory; `repo_root` opts out (#707).

Tests used to write `*.db`, `*_pixels.bin` and `*.lock` wherever pytest
was started, which is why two runs in one tree collided and why the
3.14t gate needed a `git archive` copy. The fixture under test moves
the working directory, so a stray relative write lands in `tmp_path`.
"""
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]


def test_a_test_starts_in_its_own_tmp_path(tmp_path):
    assert Path.cwd().resolve() == tmp_path.resolve()


def test_a_relative_write_lands_in_tmp_path(tmp_path):
    Path("stray.db").write_bytes(b"x")
    assert (tmp_path / "stray.db").exists()


@pytest.mark.repo_root
def test_a_repo_root_test_is_left_where_pytest_was_started(request, tmp_path):
    started = Path(request.config.invocation_params.dir).resolve()
    assert Path.cwd().resolve() == started
    assert Path.cwd().resolve() != tmp_path.resolve()


def test_a_spawned_worker_inherits_the_tests_directory(tmp_path):
    # `os.getcwd` is a builtin, so it pickles without this module being
    # importable in the child. spawn, because that is what the session's
    # pool uses and fork would inherit the cwd trivially.
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=1, mp_context=ctx) as pool:
        child_cwd = pool.submit(os.getcwd).result(timeout=120)
    assert Path(child_cwd).resolve() == tmp_path.resolve()


def test_a_module_scoped_fixture_logs_outside_the_root(pytester, monkeypatch):
    """A fixture wider than a test runs outside its `tmp_path`.

    It is set up between tests, where the cwd is the root and, before
    #707, `ISOCENTER_LOG_FILE` had been deleted by the previous test's
    `redirect_logging` -- so a module-scoped fixture that opened a
    `Session` wrote `isocenter.log` into the repository root (measured:
    `test_private_tag_vr_roundtrip.py`'s `reloaded` fixture). A session
    default keeps that log in scratch, and `redirect_logging` restores it.

    Run as its own session in a subprocess, with the variable this
    session set removed, so the inner run has a first test and a gap
    after it whatever order, `-k` or shard the outer run uses (#720
    review: in-process, it passed against the reverted fix when run
    alone). Two module fixtures: one set up before any test (the session
    default) and one set up after a test's teardown (the restore).
    """
    import shutil

    tests_dir = Path(__file__).resolve().parent
    pytester.makeconftest((tests_dir / "conftest.py").read_text(encoding="utf-8"))
    shutil.copytree(tests_dir / "support", pytester.path / "support")
    pytester.makepyfile(test_module_fixture_log="""
        import os
        from pathlib import Path

        import pytest


        def _outside(root):
            target = os.environ.get("ISOCENTER_LOG_FILE")
            assert target, "no ISOCENTER_LOG_FILE outside a test"
            # pytester puts this run's basetemp inside its rootdir, so the
            # claim is the defect's own shape: not a log in the root.
            assert Path(target).is_absolute()
            assert Path(target).resolve().parent != Path(root).resolve()
            return target


        @pytest.fixture(scope="module")
        def before_any_test(request):
            return _outside(request.config.rootpath)


        @pytest.fixture(scope="module")
        def after_a_test(request):
            return _outside(request.config.rootpath)


        def test_first(before_any_test):
            pass


        def test_second(after_a_test):
            pass
    """)
    monkeypatch.delenv("ISOCENTER_LOG_FILE", raising=False)

    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")

    result.assert_outcomes(passed=2)


def test_a_python_subprocess_imports_the_tree_under_test(tmp_path):
    """A `python -c` child started from the test's directory (#720 review).

    Five tests start `python -c`/`-m` children that import `isocenter` and
    pass no `cwd=`. Before #707 the child's cwd was the root, so `''` on
    its `sys.path` found the tree under test. From `tmp_path` it falls
    through to the editable install, which is the main checkout, so in a
    worktree without `PYTHONPATH` the child tests `main` while its parent
    tests the worktree. `conftest.py` prepends the tree to `PYTHONPATH`.
    """
    import subprocess
    import sys

    repo = Path(__file__).resolve().parent.parent
    child = subprocess.run(
        [sys.executable, "-c", "import isocenter; print(isocenter.__file__)"],
        capture_output=True, text=True, timeout=120, check=True)
    assert Path(child.stdout.strip()).resolve() == (
        repo / "isocenter" / "__init__.py").resolve()

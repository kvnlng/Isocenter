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


@pytest.fixture(scope="module")
def _log_file_a_module_fixture_sees():
    return os.environ.get("ISOCENTER_LOG_FILE")


def test_a_module_scoped_fixture_logs_outside_the_root(
        _log_file_a_module_fixture_sees, request):
    """A fixture wider than a test runs outside its `tmp_path`.

    It is set up between tests, where the cwd is the root and, before
    #707, `ISOCENTER_LOG_FILE` had been deleted by the previous test's
    `redirect_logging` -- so a module-scoped fixture that opened a
    `Session` wrote `isocenter.log` into the repository root (measured:
    `test_private_tag_vr_roundtrip.py`'s `reloaded` fixture). A session
    default keeps that log in scratch too.
    """
    target = _log_file_a_module_fixture_sees
    assert target, "no ISOCENTER_LOG_FILE outside a test: logs go to ./isocenter.log"
    root = Path(request.config.rootpath).resolve()
    assert root not in Path(target).resolve().parents

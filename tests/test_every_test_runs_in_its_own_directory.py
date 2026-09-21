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

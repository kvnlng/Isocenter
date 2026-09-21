"""A run that leaves a new file in the repository root fails (#707).

The chdir fixture moved the stray writes; this is what stops the next
one. It fails the *run*, naming the entry, because by session finish
the test that wrote it is no longer identifiable from the listing.
"""
from support import root_guard

pytest_plugins = ["pytester"]


def test_an_unchanged_root_reports_nothing(tmp_path):
    (tmp_path / "setup.py").write_text("")
    before = root_guard.snapshot(tmp_path)
    assert root_guard.new_entries(tmp_path, before) == []


def test_a_new_file_is_named(tmp_path):
    before = root_guard.snapshot(tmp_path)
    (tmp_path / "stray.db").write_bytes(b"")
    (tmp_path / "stray_pixels.bin.lock").write_bytes(b"")
    assert root_guard.new_entries(tmp_path, before) == [
        "stray.db", "stray_pixels.bin.lock"]


def test_tooling_artifacts_are_not_reported(tmp_path):
    before = root_guard.snapshot(tmp_path)
    for name in (".pytest_cache", "__pycache__", ".coverage",
                 ".coverage.host.123.abc", ".test-map.json"):
        (tmp_path / name).mkdir() if "cache" in name else (
            tmp_path / name).write_bytes(b"")
    assert root_guard.new_entries(tmp_path, before) == []


def test_the_packaging_builds_directories_are_allowed_by_exact_name(tmp_path):
    """`build/` and `isocenter.egg-info/` are setuptools' working
    directories for test_packaging_contract.py's build; a test's own
    `build.db` is still a stray."""
    before = root_guard.snapshot(tmp_path)
    (tmp_path / "build").mkdir()
    (tmp_path / "isocenter.egg-info").mkdir()
    (tmp_path / "build.db").write_bytes(b"")
    (tmp_path / "isocenter.egg-info.bak").write_bytes(b"")
    assert root_guard.new_entries(tmp_path, before) == [
        "build.db", "isocenter.egg-info.bak"]


def test_a_file_that_was_already_there_is_not_reported(tmp_path):
    (tmp_path / "old.db").write_bytes(b"")
    before = root_guard.snapshot(tmp_path)
    (tmp_path / "old.db").write_bytes(b"changed")
    assert root_guard.new_entries(tmp_path, before) == []


def test_a_run_that_writes_into_its_root_fails_naming_the_entry(pytester):
    """The wiring, not just the functions: `conftest.py` fails the run.

    The unit tests above would stay green if `pytest_sessionfinish` stopped
    calling them. This runs this very `conftest.py` in a scratch rootdir,
    with a `repo_root` test that writes into that root, in a subprocess so
    the inner run's hooks are its own.
    """
    import shutil
    from pathlib import Path

    tests_dir = Path(__file__).resolve().parent
    pytester.makeconftest((tests_dir / "conftest.py").read_text(encoding="utf-8"))
    shutil.copytree(tests_dir / "support", pytester.path / "support")
    pytester.makeini("[pytest]\nmarkers =\n    repo_root: opt out\n")
    pytester.makepyfile(test_writes_root="""
        import pytest


        @pytest.mark.repo_root
        def test_writes(request):
            (request.config.rootpath / "zz_stray.db").write_bytes(b"")


        def test_writes_relatively():
            open("zz_contained.db", "wb").close()
    """)

    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")

    result.assert_outcomes(passed=2)
    assert result.ret == 1, result.stdout.str()
    result.stdout.fnmatch_lines(
        ["*left new entries in the repository root: zz_stray.db -- *"])
    assert not (pytester.path / "zz_contained.db").exists()

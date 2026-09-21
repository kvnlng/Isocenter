"""A run that writes into the repository root fails (#707).

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


def test_a_rewritten_file_that_was_already_there_is_named_as_modified(tmp_path):
    """A stray write into a name already in the root (#720 review).

    A checkout that predates #707 holds `isocenter.log` and a dozen
    `test_*.db`/`*_pixels.bin`/`*.lock` names in its root -- exactly where a
    relative write would go. Comparing names alone passed such a write.
    """
    (tmp_path / "old.db").write_bytes(b"")
    before = root_guard.snapshot(tmp_path)
    (tmp_path / "old.db").write_bytes(b"changed")
    assert root_guard.new_entries(tmp_path, before) == []
    assert root_guard.modified_entries(tmp_path, before) == ["old.db"]


def test_a_directory_whose_contents_change_is_not_modified(tmp_path):
    """Only files: a directory's mtime moves whenever anything inside it
    is created -- `tests/__pycache__`, `.git/index.lock` -- which is not a
    write into the root."""
    (tmp_path / "tests").mkdir()
    before = root_guard.snapshot(tmp_path)
    (tmp_path / "tests" / "__pycache__").mkdir()
    assert root_guard.modified_entries(tmp_path, before) == []


def test_allowed_files_are_not_reported_as_modified(tmp_path):
    (tmp_path / ".coverage").write_bytes(b"")
    before = root_guard.snapshot(tmp_path)
    (tmp_path / ".coverage").write_bytes(b"combined data")
    assert root_guard.modified_entries(tmp_path, before) == []


def test_a_clean_root_has_no_report_line(tmp_path):
    before = root_guard.snapshot(tmp_path)
    assert root_guard.report(tmp_path, before) is None


def test_the_report_line_names_new_and_modified_entries(tmp_path):
    (tmp_path / "old.log").write_bytes(b"")
    before = root_guard.snapshot(tmp_path)
    (tmp_path / "old.log").write_bytes(b"more")
    (tmp_path / "stray.db").write_bytes(b"")
    assert root_guard.report(tmp_path, before) == (
        "this run wrote into the repository root: stray.db (new), "
        "old.log (modified) -- a test wrote outside its tmp_path (#707)")


def test_a_run_that_writes_into_its_root_fails_naming_the_entry(pytester):
    """The wiring, not just the functions: `conftest.py` fails the run.

    The unit tests above would stay green if `pytest_sessionfinish` stopped
    calling them. This runs this very `conftest.py` in a scratch rootdir,
    with `repo_root` tests that write into that root, in a subprocess so
    the inner run's hooks are its own.

    The guard's line must be the run's **last** line: `RELEASING.md` step 3
    records a run by its last line, and pytest's summary prints after
    `pytest_sessionfinish`, so a line written only there sat above a green
    `N passed` (#720 review). `pytest_unconfigure` repeats it.
    """
    import shutil
    import textwrap
    from pathlib import Path

    # A project below pytester's directory, which holds the subprocess's
    # own `stdout`/`stderr` capture files -- growing, and not the guard's
    # business.
    tests_dir = Path(__file__).resolve().parent
    proj = pytester.mkdir("proj")
    (proj / "conftest.py").write_text(
        (tests_dir / "conftest.py").read_text(encoding="utf-8"),
        encoding="utf-8")
    shutil.copytree(tests_dir / "support", proj / "support")
    (proj / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n    repo_root: opt out\n", encoding="utf-8")
    (proj / "zz_old.log").write_bytes(b"")
    (proj / "test_writes_root.py").write_text(textwrap.dedent("""
        import pytest


        @pytest.mark.repo_root
        def test_writes(request):
            (request.config.rootpath / "zz_stray.db").write_bytes(b"")


        @pytest.mark.repo_root
        def test_rewrites(request):
            (request.config.rootpath / "zz_old.log").write_bytes(b"again")


        def test_writes_relatively():
            open("zz_contained.db", "wb").close()
    """), encoding="utf-8")

    result = pytester.runpytest_subprocess(
        "-p", "no:cacheprovider", "--rootdir", str(proj),
        "-c", str(proj / "pytest.ini"), str(proj))

    result.assert_outcomes(passed=3)
    assert result.ret == 1, result.stdout.str()
    guard_line = ("this run wrote into the repository root: zz_stray.db "
                  "(new), zz_old.log (modified) -- a test wrote outside its "
                  "tmp_path (#707)")
    printed = [line for line in result.stdout.lines if line.strip()]
    assert printed[-1] == guard_line, result.stdout.str()
    assert not (proj / "zz_contained.db").exists()

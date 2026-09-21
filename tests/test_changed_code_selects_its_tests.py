"""`pytest --changed` runs the tests that exercise what was edited (#707).

Since 2026-09-17 this selection is the pre-merge check (RELEASING.md,
step 3): the full suite runs when a release is cut, not before a merge.
A test this misses lets a regression onto `main`, so every doubt widens
-- to the module's TARGETS row, then to the suite -- and several tests
below are counterexamples an adversarial review found against an
earlier, narrower design (PR #719).

pytest-testmon was tried first and rejected: it traces the pytest
process only, so an edit to `ingest_worker` selected no tests at all.
The last three tests in this file are that experiment, kept.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
# By path and appended, never inserted at 0: a future scripts/<name>.py
# must not be able to shadow a stdlib module inside the pytest process.
sys.path.append(str(REPO / "scripts"))
import test_map  # noqa: E402

SOURCE = '''\
import os

LIMIT = 3


class Session:
    flag = True

    def compact(self):
        """Doc."""
        return 1

    def export(self):
        def inner():
            return 2
        return inner()

    def never_called(
            self,
            mode=dict(x=1)):
        return 3


def scan_worker(args):
    return args
'''

def test_functions_are_named_the_way_a_reader_would_name_them():
    assert test_map.functions_in(SOURCE) == [
        ("Session.compact", 9, 11, 10), ("Session.export", 13, 16, 14),
        ("Session.export.inner", 14, 15, 15), ("Session.never_called", 18, 21, 21),
        ("scan_worker", 24, 25, 25)]

def test_a_line_belongs_to_its_innermost_function_or_to_none():
    assert test_map.function_at(SOURCE, 11) == "Session.compact"
    assert test_map.function_at(SOURCE, 15) == "Session.export.inner"
    assert test_map.function_at(SOURCE, 16) == "Session.export"
    assert test_map.function_at(SOURCE, 3) is None
    assert test_map.function_at(SOURCE, 7) is None

def test_functions_split_into_what_tests_ran_and_what_workers_ran():
    contexts = {9: ["<startup>"], 11: ["tests/test_c.py::test_a[x]", ""],
                18: ["<startup>"], 20: ["<startup>"], 24: [""], 25: [""],
                15: ["<startup>", "tests/test_c.py::test_b"],
                3: ["tests/test_c.py::test_a"]}
    functions, workers = test_map.split_functions(SOURCE, contexts)
    assert functions == {"Session.compact": ["tests/test_c.py::test_a"],
                         "Session.export.inner": ["tests/test_c.py::test_b"]}
    assert workers == ["Session.compact", "scan_worker"]

def test_a_signature_is_not_a_call():
    assert test_map.split_functions(SOURCE, {18: [""], 19: [""], 20: [""]}) == ({}, [])

DIFF = """\
diff --git a/isocenter/session.py b/isocenter/session.py
--- a/isocenter/session.py
+++ b/isocenter/session.py
@@ -70,0 +71 @@ def scan_worker(args):
+    probe = 1
@@ -1612,2 +1613,1 @@ class DicomSession:
-        a = 1
-        b = 2
+        a = 3
@@ -1700,2 +1699,0 @@ class DicomSession:
-        gone = 1
-        gone = 2
diff --git a/isocenter/old.py b/isocenter/old.py
--- a/isocenter/old.py
+++ /dev/null
@@ -1,2 +0,0 @@
-x
-y
"""

def test_each_side_of_a_hunk_is_read_in_its_own_numbering():
    old, new = test_map.parse_hunks(DIFF)
    assert old == {"isocenter/session.py": [(1612, 1613), (1700, 1701)],
                   "isocenter/old.py": [(1, 2)]}
    assert new == {"isocenter/session.py": [(71, 71), (1613, 1613)]}

@pytest.fixture
def repo(tmp_path):
    def git(*a): subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)
    git("init", "-q", "-b", "main"); git("config", "user.email", "t@t"); git("config", "user.name", "t")
    (tmp_path / "isocenter").mkdir(); (tmp_path / "tests").mkdir()
    (tmp_path / "isocenter" / "a.py").write_text("def f():\n    return 1\n\n\ndef g():\n    return 2\n\n\ndef h():\n    return 3\n")
    (tmp_path / "isocenter" / "b.py").write_text("def k():\n    return 1\n")
    (tmp_path / "tests" / "test_a.py").write_text("def test_f(): pass\n")
    git("add", "-A"); git("commit", "-qm", "base")
    return tmp_path, git

def test_deleting_a_function_names_it(repo):
    path, git = repo
    (path / "isocenter" / "a.py").write_text("def f():\n    return 1\n\n\ndef h():\n    return 3\n")
    changes, other = test_map.changed(path, "HEAD")
    assert test_map.Change("isocenter/a.py", "g") in changes
    assert test_map.Change("isocenter/a.py", "f") not in changes

def test_a_rename_is_a_delete_and_an_add(repo):
    path, git = repo
    git("mv", "isocenter/b.py", "isocenter/c.py")
    changes, other = test_map.changed(path, "HEAD")
    # No rename pairing: the old name's functions are found on the old
    # side and the new name's on the new, so both sets of tests run.
    assert changes == {test_map.Change("isocenter/b.py", "k"),
                       test_map.Change("isocenter/c.py", "k")}
    assert other == []

def test_git_config_cannot_blind_it(repo):
    path, git = repo
    git("config", "diff.noprefix", "true")
    (path / "isocenter" / "a.py").write_text("def f():\n    return 10\n\n\ndef g():\n    return 2\n\n\ndef h():\n    return 3\n")
    changes, _ = test_map.changed(path, "HEAD")
    assert changes == {test_map.Change("isocenter/a.py", "f")}

def test_a_deleted_line_that_looks_like_a_header_does_not_switch_files():
    spoof = ("diff --git a/isocenter/a.py b/isocenter/a.py\n"
             "--- a/isocenter/a.py\n+++ b/isocenter/a.py\n"
             "@@ -3,2 +3,1 @@\n"
             "--- a/isocenter/zzz.py\n-x = 1\n+y = 2\n"
             "@@ -9 +8 @@\n-p\n+q\n")
    old, new = test_map.parse_hunks(spoof)
    assert set(old) == set(new) == {"isocenter/a.py"}


def test_a_module_that_does_not_parse_falls_to_its_row():
    assert test_map.changes_in("isocenter/a.py", "def broken(:\n", [(1, 1)]) == {
        test_map.Change("isocenter/a.py", None)}


def test_a_one_line_function_is_counted_as_run_by_a_worker():
    # Its signature and body share a line, so an import in a spawned
    # process files it under workers: an edit then selects the
    # dispatchers' tests (rule 2) instead of its row (rule 3).
    source = "def f(): return 1\n"
    assert test_map.split_functions(source, {1: [""]}) == ({}, ["f"])


def test_the_package_has_no_one_line_function():
    # Why the case above is accepted rather than handled.
    one_line = [
        f"{path.relative_to(REPO)}::{name}"
        for path in sorted((REPO / "isocenter").rglob("*.py"))
        for name, _def, _end, body in test_map.functions_in(
            path.read_text(encoding="utf-8"))
        if body == _def]
    assert one_line == [], (
        "a one-line def is filed as worker-run whenever a spawned process "
        "imports its module; see split_functions before adding one")


def test_paths_with_spaces_and_modes(repo):
    path, git = repo
    (path / "tests" / "test new.py").write_text("x = 1\n")
    (path / "isocenter" / "b.py").chmod(0o755)
    _, other = test_map.changed(path, "HEAD")
    assert "tests/test new.py" in other and "isocenter/b.py" in other

MAP = {"sha": "abc", "python": "3.14.7t",
       "functions": {"isocenter/session.py": {
           "DicomSession.compact": ["tests/test_compaction.py::TestCompaction::test_a"],
           "DicomSession.ingest": ["tests/test_multiprocessing.py::test_parallel"],
           "helper": ["tests/test_unit.py::test_helper"]}},
       "workers": {"isocenter/io_handlers.py": ["_export_instance_worker",
                                                "ingest_worker"],
                   "isocenter/session.py": ["helper"]},
       "unmapped": []}
TARGETS = {"isocenter/session.py": (["tests/test_session.py", "tests/test_new.py"], 80),
           "isocenter/io_handlers.py": (["tests/test_io.py"], 80)}
#: {worker: {(path, the function that hands it to a pool)}}.
DISPATCHING = {"ingest_worker": {("isocenter/session.py", "DicomSession.ingest")}}
EXPORT_BATCH = ("isocenter/io_handlers.py", "DicomExporter.export_batch")
C = test_map.Change

def _select(changes=(), other=(), mapping=MAP, **kw):
    return test_map.select(mapping, set(changes), list(other), TARGETS, REPO,
                           dispatching=kw.pop("dispatching", DISPATCHING), **kw)

def test_rule_1_a_function_a_test_ran_selects_those_tests():
    sel = _select([C("isocenter/session.py", "DicomSession.compact")])
    assert not sel.full and not sel.files
    assert sel.nodeids == {"tests/test_compaction.py::TestCompaction::test_a"}

def test_rule_2_a_worker_function_selects_through_its_dispatchers():
    assert _select([C("isocenter/io_handlers.py", "ingest_worker")]).nodeids == {
        "tests/test_multiprocessing.py::test_parallel"}

def test_a_function_tests_and_workers_both_ran_selects_both():
    assert _select([C("isocenter/session.py", "helper")]).nodeids == {
        "tests/test_unit.py::test_helper", "tests/test_multiprocessing.py::test_parallel"}

def test_a_dispatch_at_module_scope_widens_to_the_row():
    sel = _select([C("isocenter/io_handlers.py", "ingest_worker")],
                  dispatching={"ingest_worker": DISPATCHING["ingest_worker"]
                               | {("isocenter/x.py", None)}})
    assert "tests/test_io.py" in sel.files


def test_a_workers_own_dispatcher_without_a_record_widens_to_the_row():
    # The #719 reviewer's case: export's dispatcher renamed since the
    # build, ingest's still recorded. An export-worker edit used to
    # select the ingest tests and not one export test.
    sel = _select([C("isocenter/io_handlers.py", "_export_instance_worker")],
                  dispatching=dict(DISPATCHING,
                                   _export_instance_worker={EXPORT_BATCH}))
    assert "tests/test_io.py" in sel.files
    assert "tests/test_multiprocessing.py::test_parallel" not in sel.nodeids


def test_another_workers_blind_dispatcher_does_not_widen_this_one():
    # Rule 2 per worker (#707 PR 3): an ingest_worker edit reaches its
    # tests through ingest's dispatcher alone. With the union of every
    # dispatcher, a map that never recorded export's widened every
    # worker edit to the row -- rule 2 dead for all five on a map with
    # one blind spot, and the first real-map probe went to the suite.
    sel = _select([C("isocenter/io_handlers.py", "ingest_worker")],
                  dispatching=dict(DISPATCHING,
                                   _export_instance_worker={EXPORT_BATCH}))
    assert sel.nodeids == {"tests/test_multiprocessing.py::test_parallel"}
    assert not sel.files and not sel.full


def test_a_helper_only_workers_run_uses_every_dispatcher():
    # Not itself a worker, so there is no one worker to key on: any
    # pool might reach it, and one blind dispatcher widens.
    sel = _select([C("isocenter/session.py", "helper")],
                  dispatching=dict(DISPATCHING,
                                   _export_instance_worker={EXPORT_BATCH}))
    assert {"tests/test_session.py", "tests/test_new.py"} <= sel.files
    assert "tests/test_multiprocessing.py::test_parallel" in sel.nodeids

def test_rule_3_no_record_falls_to_the_targets_row():
    assert _select([C("isocenter/session.py", "DicomSession.brand_new")]).files == {"tests/test_session.py", "tests/test_new.py"}
    assert _select([C("isocenter/session.py", None)]).files == {"tests/test_session.py", "tests/test_new.py"}

def test_rule_4_no_row_selects_the_full_suite():
    assert _select([C("isocenter/profiles.py", None)]).full

def test_rule_5_a_changed_test_file_selects_itself():
    sel = _select(other=["tests/test_crypto.py"])
    assert sel.files == {"tests/test_crypto.py"} and not sel.full

@pytest.mark.parametrize("path", ["tests/conftest.py", "tests/support/shards.py", "setup.py", "pytest.ini", ".coveragerc", "pyproject.toml", "MANIFEST.in"])
def test_rule_6_shared_machinery_selects_the_full_suite(path):
    assert _select(other=[path]).full

def test_rule_7_a_path_no_test_names_selects_the_full_suite():
    # Built at run time, or this file would be the test that names it.
    assert _select(other=[".github/workflows/" + "nobody-" + "names-this.yml"]).full
    assert _select(other=["scripts/" + "nobody_" + "names_this.py"]).full


def test_rule_7_documentation_no_test_names_selects_nothing():
    # Every dated spec has a basename no test names. Sending those to the
    # suite is the per-PR full run the 2026-09-17 ruling ended -- found
    # when the PR that wrote this rule selected the suite for itself.
    for path in ("docs/superpowers/specs/" + "nobody-" + "names-this.md",
                 "NOBODY_" + "NAMES_THIS.md"):
        sel = _select(other=[path])
        assert not sel.full and not sel.files and not sel.nodeids
        assert "nothing" in sel.reasons[-1]


def test_rule_7_matches_a_python_file_by_its_stem_and_nothing_else_by_it(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("import helper_mod\nsession = 1\n")
    named = test_map._tests_naming(tmp_path, "scripts/helper_mod.py")
    assert named == {"tests/test_x.py"}
    assert test_map._tests_naming(tmp_path, "docs/session.md") == set()

def test_package_data_selects_the_full_suite():
    assert _select(other=["isocenter/resources/redaction_rules.json"]).full

def test_a_module_with_no_hunk_falls_to_its_row_or_the_suite():
    assert _select(other=["isocenter/session.py"]).files == {"tests/test_session.py", "tests/test_new.py"}
    assert _select(other=["isocenter/brand_new.py"]).full

def test_no_map_degrades_to_targets_rows_and_says_so():
    sel = _select([C("isocenter/session.py", "DicomSession.compact")], mapping=None)
    assert "tests/test_session.py" in sel.files
    assert any("no usable map" in r for r in sel.reasons)

def test_unspoken_widens_within_rows_only():
    sel = _select([C("isocenter/session.py", "DicomSession.compact")],
                  unspoken={"tests/test_new.py", "tests/test_elsewhere.py"})
    assert sel.files == {"tests/test_new.py"}

def test_a_selected_test_that_no_longer_exists_is_reported():
    sel = _select([C("isocenter/session.py", "DicomSession.compact")])
    assert test_map.unmatched(sel, ["tests/test_other.py::test_x[1]"]) == sel.nodeids
    assert not test_map.unmatched(sel, ["tests/test_compaction.py::TestCompaction::test_a[p]"])

def test_an_old_map_names_what_it_cannot_speak_for(repo):
    path, git = repo
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True).stdout.strip()
    (path / "isocenter" / "a.py").write_text("def f():\n    return g()\n\n\ndef g():\n    return 2\n\n\ndef h():\n    return 3\n")
    (path / "tests" / "test_added.py").write_text("def test_n(): pass\n")
    git("add", "-A"); git("commit", "-qm", "main moved")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True).stdout.strip()
    mapping = {"sha": sha, "functions": {"isocenter/a.py": {"f": ["tests/test_f.py::test_f"], "g": ["tests/test_g.py::test_g"]}},
               "workers": {}, "unmapped": ["tests/test_skipped.py::test_s"]}
    assert test_map.cannot_speak_for(mapping, path, base) == {
        "tests/test_skipped.py", "tests/test_added.py", "tests/test_f.py"}


def test_the_dispatch_finder_sees_every_worker_in_the_live_source():
    found = test_map.dispatched_workers(REPO)
    assert {"scan_worker", "_verify_worker", "_discover_worker",
            "ingest_worker", "_export_instance_worker"} <= found, (
        "a worker function has no hand-off the finder recognises, so an "
        "edit to it would fall past rule 2")
    assert all(name for handoffs in test_map.dispatchers(REPO).values()
               for _path, name in handoffs), (
        "a worker is handed to a pool at module scope; rule 2 cannot "
        "reach its tests and widens to the row instead -- decide whether "
        "that is wanted before accepting it")


def _git_tree(path):
    return subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                          cwd=path, capture_output=True).returncode == 0


def test_select_prints_its_reasons_and_what_it_is_for():
    if not _git_tree(REPO):
        pytest.skip("not a git work tree: a `git archive` copy has no diff")
    out = subprocess.run(
        [sys.executable, "-m", "scripts.test_map", "select"],
        cwd=REPO, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "the pre-merge check (RELEASING.md step 3)" in out.stdout
    assert "the full suite runs when a release is cut" in out.stdout


def test_a_selected_test_that_is_gone_sends_its_modules_to_their_rows():
    sel = _select([C("isocenter/session.py", "DicomSession.compact")])
    test_map.fall_back_for_missing(sel, set(sel.nodeids), TARGETS)
    assert {"tests/test_session.py", "tests/test_new.py"} <= sel.files
    assert any("no longer exist" in reason for reason in sel.reasons)


def test_a_build_whose_suite_failed_exits_with_the_suites_status(
        tmp_path, monkeypatch):
    """`build` is "Cutting a release" step 1's 3.14t integration run
    (#707). It still writes the map when a test fails, but must exit with
    pytest's status: a build that swallowed it would record a red
    integration run as `exit=0`."""
    class Done:
        def __init__(self, returncode, stdout=""):
            self.returncode, self.stdout = returncode, stdout

    def run(cmd, **kwargs):
        if "--collect-only" in cmd:
            return Done(0, "tests/test_x.py::test_a\n")
        return Done(1 if "run" in cmd else 0)

    monkeypatch.setattr(test_map.subprocess, "run", run)
    monkeypatch.setattr(test_map, "from_coverage",
                        lambda *a, **k: {"functions": {}, "workers": {},
                                         "unmapped": []})
    with pytest.raises(SystemExit) as stopped:
        test_map.main(["build", "--sha", "abc", "--out", str(tmp_path)])
    assert stopped.value.code == 1
    assert (tmp_path / test_map.MAP_FILE).exists()


def test_a_map_whose_commit_is_not_here_is_no_map(tmp_path):
    (tmp_path / test_map.MAP_FILE).write_text(
        '{"sha": "0000000000000000000000000000000000000000", "python": "x", '
        '"functions": {}, "workers": {}, "unmapped": []}')
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    assert test_map.load(tmp_path) is None


@pytest.mark.parametrize("base_args", [
    ["--changed-base=main"], ["--changed-base", "main"]])
def test_a_vanished_test_widens_whichever_way_the_base_is_spelled(
        tmp_path, base_args):
    """`pytest --changed` end to end, in a scratch repository (#707).

    The map names a test that no longer exists. Against a whole
    collection, that sends the touched module to its TARGETS row. The
    check used to be skipped whenever an argument did not start with
    `-`, so `--changed-base main` -- the spelling RELEASING.md used --
    or `-p no:cacheprovider` read as a path, and the run selected
    nothing and exited 5 (#719 review, carried into #707's PR 3).
    """
    import json
    import shutil
    if not _git_tree(REPO):
        pytest.skip("not a git work tree: a `git archive` copy has no diff")
    proj = tmp_path / "proj"
    for sub in ("tests", "scripts", "isocenter"):
        (proj / sub).mkdir(parents=True)
    shutil.copy(REPO / "pytest.ini", proj / "pytest.ini")
    shutil.copy(REPO / "tests" / "conftest.py", proj / "tests")
    shutil.copytree(REPO / "tests" / "support", proj / "tests" / "support")
    shutil.copy(REPO / "scripts" / "test_map.py", proj / "scripts")
    (proj / "scripts" / "__init__.py").write_text("")
    (proj / "scripts" / "mutation_probe.py").write_text(
        'TARGETS = {"isocenter/extra.py": (["tests/test_one.py"], 1)}\n')
    # No __init__.py: a namespace portion, so `import isocenter` in the
    # copied conftest still finds the real package on PYTHONPATH.
    (proj / "isocenter" / "extra.py").write_text("def f():\n    return 1\n")
    (proj / "tests" / "test_one.py").write_text("def test_a():\n    pass\n")
    (proj / ".gitignore").write_text(".test-map.json\n.coverage*\n")

    def git(*args):
        return subprocess.run(["git", *args], cwd=proj, check=True,
                              capture_output=True, text=True).stdout.strip()
    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    git("add", "-A")
    git("commit", "-qm", "base")
    (proj / test_map.MAP_FILE).write_text(json.dumps({
        "sha": git("rev-parse", "HEAD"), "python": "x",
        "functions": {"isocenter/extra.py": {"f": ["tests/test_gone.py::test_x"]}},
        "workers": {}, "unmapped": []}))
    (proj / "isocenter" / "extra.py").write_text("def f():\n    return 2\n")

    env = dict(os.environ, PYTHONPATH=str(REPO), PYTHONDONTWRITEBYTECODE="1")
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "-p", "no:cacheprovider", "--changed", *base_args],
        cwd=proj, env=env, capture_output=True, text=True, timeout=300)
    assert "no longer exist" in out.stdout, out.stdout + out.stderr
    assert "tests/test_one.py::test_a" in out.stdout, out.stdout
    assert out.returncode == 0, out.stdout + out.stderr


PROBE_FILES = ["tests/test_multiprocessing.py", "tests/test_compaction.py",
               "tests/test_crypto.py"]


@pytest.fixture(scope="module")
def small_real_map(tmp_path_factory):
    """A map built from three real test files, in a scratch copy."""
    import os
    try:
        import coverage  # noqa: F401
    except ImportError:
        pytest.skip("coverage is in the dev extra")
    if not _git_tree(REPO):
        pytest.skip("not a git work tree: nothing to `git archive`")
    proj = tmp_path_factory.mktemp("maprepo")
    subprocess.run(f"git archive HEAD | tar -x -C {proj}", shell=True,
                   cwd=REPO, check=True)
    rc = proj / "both.rc"
    rc.write_text((proj / ".coveragerc").read_text().replace(
        "\nconcurrency = multiprocessing\n",
        "\nconcurrency = multiprocessing,thread\n"))
    env = {k: v for k, v in os.environ.items() if not k.startswith("COVERAGE_")}
    env.update(PYTHONPATH=str(proj), PYTHONDONTWRITEBYTECODE="1",
               COVERAGE_FILE=str(proj / ".coverage"), TEST_MAP_CONTEXTS="1")
    subprocess.run([sys.executable, "-m", "coverage", "run", f"--rcfile={rc}",
                    "-m", "pytest", "-q", *PROBE_FILES],
                   cwd=proj, env=env, check=True, timeout=1500)
    subprocess.run([sys.executable, "-m", "coverage", "combine",
                    f"--rcfile={rc}"], cwd=proj, env=env, check=True)
    return proj, test_map.from_coverage(proj / ".coverage", proj, "HEAD", "probe")


def _files(sel):
    return {n.split("::")[0] for n in sel.nodeids} | sel.files


def test_a_compact_edit_selects_the_compaction_tests_only(small_real_map):
    proj, mapping = small_real_map
    sel = test_map.select(
        mapping, {C("isocenter/session.py", "DicomSession.compact")}, [],
        {}, proj)
    assert not sel.full, sel.reasons
    assert _files(sel) == {"tests/test_compaction.py"}


@pytest.mark.parametrize("path, qualname", [
    ("isocenter/session.py", "scan_worker"),
    ("isocenter/io_handlers.py", "ingest_worker"),
])
def test_a_worker_edit_selects_the_test_that_runs_it_in_a_pool(
        small_real_map, path, qualname):
    # pytest-testmon selected 0 of 28 for the ingest_worker edit.
    proj, mapping = small_real_map
    sel = test_map.select(mapping, {C(path, qualname)}, [], {}, proj)
    assert not sel.full, sel.reasons
    assert "tests/test_multiprocessing.py" in _files(sel)
    assert "tests/test_crypto.py" not in _files(sel)

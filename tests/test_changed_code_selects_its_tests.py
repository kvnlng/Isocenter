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
    # That the package has none is checked in
    # tests/test_the_selector_reads_the_live_source.py.


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
    # The glob, source-reader and wide-fixture detectors read the real
    # tests/; each has its own tests (the source readers' are in
    # test_the_selector_reads_the_live_source.py), so the rule tests here
    # see none of them.
    kw.setdefault("readers", lambda path: set())
    kw.setdefault("by_name", lambda path: set())
    kw.setdefault("wide", lambda test_file: False)
    return test_map.select(mapping, set(changes), list(other), TARGETS,
                           kw.pop("repo", REPO),
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

def test_a_workers_method_is_matched_by_its_last_name_part():
    # `run_parallel(service.execute_redaction_task, ...)` hands on the
    # method `RedactionService.execute_redaction_task` (#734 review).
    mapping = dict(MAP, workers={"isocenter/services.py": [
        "RedactionService.execute_redaction_task"]})
    sel = _select([C("isocenter/services.py",
                     "RedactionService.execute_redaction_task")],
                  mapping=mapping,
                  dispatching={"execute_redaction_task": {
                      ("isocenter/session.py", "DicomSession.ingest")}})
    assert sel.nodeids == {"tests/test_multiprocessing.py::test_parallel"}
    assert not sel.files and not sel.full


def test_a_helper_with_no_record_of_its_own_takes_its_row_too():
    # `parallel._worker_init`: every pool runs it, no test ran it
    # in-process, and the dispatchers' tests missed 3 of its row's 13
    # files, among them the ones that pin the initializer (#734 review).
    mapping = dict(MAP, workers={"isocenter/session.py": ["_only_in_workers"]})
    sel = _select([C("isocenter/session.py", "_only_in_workers")],
                  mapping=mapping)
    assert "tests/test_multiprocessing.py::test_parallel" in sel.nodeids
    assert {"tests/test_session.py", "tests/test_new.py"} <= sel.files


def test_rule_3_no_record_falls_to_the_targets_row():
    assert _select([C("isocenter/session.py", "DicomSession.brand_new")]).files == {"tests/test_session.py", "tests/test_new.py"}
    assert _select([C("isocenter/session.py", None)]).files == {"tests/test_session.py", "tests/test_new.py"}

def test_rule_4_no_row_selects_the_full_suite():
    assert _select([C("isocenter/profiles.py", None)]).full

def test_rule_5_a_changed_test_file_selects_itself_and_its_importers(tmp_path):
    # `test_export_failure_audit.py`'s `_session` is imported by three
    # other test files; an edit to it selected that file only (#734
    # review, finding 3).
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_helpers.py").write_text("def _session(): pass\n")
    (tmp_path / "tests" / "test_user.py").write_text(
        "from tests.test_helpers import _session\n")
    (tmp_path / "tests" / "test_other.py").write_text("x = 1\n")
    sel = _select(other=["tests/test_helpers.py"], repo=tmp_path)
    assert sel.files == {"tests/test_helpers.py", "tests/test_user.py"}
    assert not sel.full

@pytest.mark.parametrize("path", ["tests/conftest.py", "tests/support/shards.py", "setup.py", "pytest.ini", ".coveragerc", "pyproject.toml", "MANIFEST.in"])
def test_rule_6_shared_machinery_selects_the_full_suite(path):
    assert _select(other=[path]).full

def test_rule_7_a_path_no_test_names_selects_the_full_suite():
    # Built at run time, or this file would be the test that names it.
    assert _select(other=[".github/workflows/" + "nobody-" + "names-this.yml"]).full
    assert _select(other=["scripts/" + "nobody_" + "names_this.py"]).full


def test_rule_7_documentation_no_test_names_selects_only_its_glob_readers():
    # Every dated spec has a basename no test names. Sending those to the
    # suite is the per-PR full run the 2026-09-17 ruling ended -- found
    # when the PR that wrote this rule selected the suite for itself. But
    # "nothing" rested on "no test reads prose", and four read every page
    # by glob: a broken anchor in a page no test names exited 5 (#734).
    # (Named here, the page would select this file for every edit to it.)
    for path in ("docs/superpowers/specs/" + "nobody-" + "names-this.md",
                 "NOBODY_" + "NAMES_THIS.md"):
        sel = _select(other=[path])
        assert not sel.full and not sel.files and not sel.nodeids
        sel = _select(other=[path], readers=lambda p: {"tests/test_doc_anchors.py"})
        assert sel.files == {"tests/test_doc_anchors.py"} and not sel.full
        assert any("by glob" in reason for reason in sel.reasons)


def test_the_glob_detector_wants_a_glob_and_a_matching_pattern(tmp_path):
    star = "*"
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_reads.py").write_text(
        f'for p in root.rglob("{star}.md"): pass\n')
    (tmp_path / "tests" / "test_says.py").write_text(f'X = "{star}.md"\n')
    (tmp_path / "tests" / "test_other.py").write_text(
        f'for p in out.rglob("{star}.dcm"): pass\n')
    assert test_map.glob_readers(tmp_path, "docs/a.md") == {"tests/test_reads.py"}


@pytest.mark.parametrize("call", ["os." + "walk(root)", "os." + "listdir(root)",
                                  "os." + "scandir(root)", "root." + "iterdir()",
                                  "root." + "rglob('" + "*')",
                                  "glob." + "glob(root)", "glob." + "iglob(root)"])
def test_the_detector_sees_a_walk_filtered_by_suffix(tmp_path, call):
    # A test that walks a tree and keeps one suffix reads every file of
    # that kind without a glob literal: `test_equipment_has_one_constructor
    # _outside_entities` reads every package module through os.walk and an
    # endswith on the Python suffix, and an `Equipment()` added to
    # privacy.py was not selected and failed (#744). Spelled in pieces, and
    # no suffix quoted whole even in a comment (the detector reads
    # comments too), or this file would read every page or module itself.
    md, dcm = "." + "md", "." + "dcm"
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_walks.py").write_text(
        f'for f in {call}:\n    if str(f).endswith("{md}"): pass\n')
    (tests / "test_says.py").write_text(f'SUFFIX = "{md}"\n')
    (tests / "test_other_kind.py").write_text(
        f'for f in {call}:\n    if str(f).endswith("{dcm}"): pass\n'
        f'NOTES = "notes{md}"\n')
    (tests / "test_ast.py").write_text(
        f'for n in ast.' + f'walk(tree):\n    name = "x{md}"\nS = "{md}"\n')
    # test_other_kind walks for another suffix and names one page: a
    # reader of neither every page nor that one.
    assert test_map.glob_readers(tmp_path, "docs/a.md") == {"tests/test_walks.py"}
    # A suffix may start with a digit.
    (tests / "test_archives.py").write_text(
        f'for f in {call}:\n    if str(f).endswith(".7z"): pass\n')
    assert test_map.glob_readers(tmp_path, "fixtures/a.7z") == {
        "tests/test_archives.py"}


def test_every_changed_path_adds_its_glob_readers():
    sel = _select([C("isocenter/session.py", "DicomSession.compact")],
                  readers=lambda p: {"tests/test_source_citations.py"})
    assert sel.files == {"tests/test_source_citations.py"}
    assert "tests/test_compaction.py::TestCompaction::test_a" in sel.nodeids


def test_a_test_with_a_wide_fixture_takes_its_whole_file(tmp_path):
    # A module-scoped fixture's work is recorded on its first consumer
    # only; a later consumer is dropped the day it becomes mapped (#734
    # review, finding 6).
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_compaction.py").write_text(
        '@pytest.fixture(scope="module")\ndef built(): pass\n')
    sel = _select([C("isocenter/session.py", "DicomSession.compact")],
                  wide=lambda f: test_map.has_wide_fixture(tmp_path, f))
    assert "tests/test_compaction.py" in sel.files
    assert not test_map.has_wide_fixture(tmp_path, "tests/test_absent.py")


def test_a_selection_reads_each_test_file_once_however_many_paths_changed(
        tmp_path, monkeypatch):
    # The detectors read every tests/test_*.py, and asked once per changed
    # path they read the suite again for each: release/1.0's 213 paths
    # cost 86k regex searches over 405 files, 40 of `select`'s 45 s, and
    # past 120 s under a four-shard release run (#914). The bound is on
    # reads, not seconds: a read per path per file is the cost that grew.
    import io
    star, md = "*", "." + "md"
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_walks.py").write_text(
        f'for p in root.rglob("{star}{md}"): pass\n')
    (tests / "test_names.py").write_text("import helper_mod\n")
    (tests / "test_wide.py").write_text(
        '@pytest.fixture(scope="module")\ndef built(): pass\n'
        'def test_x(): pass\n')
    mapping = {"functions": {"isocenter/a.py": {
        "f": ["tests/test_wide.py::test_x"]}}, "workers": {}}
    other = [f"docs/page{i}{md}" for i in range(6)] + [
        "scripts/helper_mod.py", "scripts/other_mod.py", "tests/test_names.py"]
    reads, real_open = {}, io.open
    # In pieces: a whole Python-suffix literal here would make this file a
    # reader of every module (`glob_readers`), and every package edit would
    # select it (test_the_selector_reads_the_live_source.py pins that).
    py = "." + "py"

    def counting_open(file, *args, **kwargs):
        name = Path(os.fspath(file)).name if isinstance(
            file, (str, os.PathLike)) else None
        if name and name.startswith("test_") and name.endswith(py):
            reads[name] = reads.get(name, 0) + 1
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(io, "open", counting_open)
    sel = test_map.select(mapping, {C("isocenter/a.py", "f")}, other, {},
                          tmp_path, dispatching={})
    # What the reads were for, so a selection that read nothing cannot pass.
    assert {"tests/test_walks.py", "tests/test_wide.py"} <= sel.files
    assert reads == {"test_walks.py": 1, "test_names.py": 1, "test_wide.py": 1}


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

def test_a_selected_test_that_no_longer_exists_is_reported(tmp_path):
    # Per file (#734 review, finding 5): gone when its file is not on disk,
    # or its file was collected and it was not; a file left out of a
    # restricted run is not evidence either way.
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_compaction.py").write_text("")
    sel = _select([C("isocenter/session.py", "DicomSession.compact")])
    node = "tests/test_compaction.py::TestCompaction::test_a"
    assert not test_map.vanished(sel, [node + "[p]"], tmp_path)
    assert not test_map.vanished(sel, ["tests/test_other.py::test_x[1]"], tmp_path)
    assert test_map.vanished(
        sel, ["tests/test_compaction.py::TestCompaction::test_b"], tmp_path) == {node}
    (tmp_path / "tests" / "test_compaction.py").unlink()
    assert test_map.vanished(sel, [], tmp_path) == {node}

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


def test_a_hand_off_is_found_by_the_call_whatever_the_argument():
    source = ("def a(service, pool):\n"
              "    run_parallel(service.execute_redaction_task, items)\n"
              "    run_parallel(func=plain, items=items)\n"
              "    pool.submit(os.getpid)\n"
              "    run_parallel(lambda x: x, items)\n"
              "    ProcessPoolExecutor(max_workers=2)\n"
              "    map(str, items)\n")
    assert sorted(test_map.pool_calls(source), key=str) == sorted([
        (2, "run_parallel", "execute_redaction_task"),
        (3, "run_parallel", "plain"), (4, "submit", "getpid"),
        (5, "run_parallel", None), (6, "ProcessPoolExecutor", None)], key=str)


def _git_tree(path):
    return subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                          cwd=path, capture_output=True).returncode == 0


def _shares_history(path, ref):
    """Whether `select`'s default path can use `ref`: HEAD and `ref` have
    a merge base. Not whether `ref` exists (#800): a clone can hold an
    `origin/main` that shares no commit with HEAD, and `select` passes it
    over exactly as it passes over a ref that is not there."""
    return subprocess.run(["git", "merge-base", "HEAD", ref],
                          cwd=path, capture_output=True).returncode == 0


def test_select_prints_its_reasons_and_what_it_is_for():
    if not _git_tree(REPO):
        pytest.skip("not a git work tree: a `git archive` copy has no diff")
    # `select` diffs from its merge base with main. A checkout of one
    # branch alone has neither `origin/main` nor `main` (the release
    # rehearsal's was one until tests.yml fetched every branch and tag,
    # #966; a single-branch clone still is), and `select` then exits
    # asking for a base, as it should. That refusal is not this test's
    # subject: with no main to find, name HEAD as the base so the output
    # is still read. Where main exists, the default path is the one run,
    # and since #966 that is the path a runner takes.
    base = ([] if any(_shares_history(REPO, r) for r in ("origin/main", "main"))
            else ["--base", "HEAD"])
    out = subprocess.run(
        [sys.executable, "-m", "scripts.test_map", "select", *base],
        cwd=REPO, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "the pre-merge check (RELEASING.md step 3)" in out.stdout
    assert "the full suite runs when a release is cut" in out.stdout


# --- `select`'s default base, in scratch repositories (#800) --------------
#
# The test above is the only one that reaches `merge_base(repo, None)`, and
# only where a main exists. Swapping the two candidates, or turning the
# fall-through into a raise together with a reworded refusal, left the 70
# tests of this file green (C5's spec, §4.1). Nothing here reads this
# checkout's refs.

class _Scratch:
    """A git repository under `tmp_path` that no developer configuration
    reaches. Its first branch is `trunk`, so no `main` exists until a test
    makes one, and `origin/main` is a ref written by hand: no remote."""

    def __init__(self, path):
        self.path = path
        path.mkdir(parents=True)
        self.git("init", "-q", "-b", "trunk")

    def git(self, *args):
        return subprocess.run(
            ["git", "-c", "user.name=scratch",
             "-c", "user.email=scratch@example.invalid",
             "-c", "commit.gpgsign=false", *args],
            cwd=self.path, check=True, capture_output=True,
            text=True).stdout.strip()

    def commit(self, name):
        (self.path / name).write_text(name, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", name)
        return self.git("rev-parse", "HEAD")


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    # A test run from a git hook inherits these, and every git below --
    # the fixture's and `test_map._git`'s -- would then talk to the
    # repository the hook is running in.
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(name, raising=False)
    made = []

    def make():
        made.append(_Scratch(tmp_path / f"scratch-{len(made)}"))
        return made[-1]
    return make


def _refusal(repo, upstream=None):
    with pytest.raises(SystemExit) as stop:
        test_map.merge_base(repo.path, upstream)
    return str(stop.value.code)


def _is_the_refusal(text):
    # Its two load-bearing phrases, not the sentence: what is missing, and
    # the argument that supplies it.
    return ("the branch this work will merge into" in text
            and "--changed-base=release/X.Y" in text)


def test_the_default_base_is_origin_main_then_main(scratch):
    """Kills: the two candidates swapped (a stale local `main` would then
    decide what a branch is diffed against); `main` dropped from the
    list."""
    repo = scratch()
    behind = repo.commit("a")
    ahead = repo.commit("b")
    repo.git("update-ref", "refs/remotes/origin/main", behind)
    repo.git("branch", "main", ahead)
    repo.commit("work")
    # The two candidates give different answers, so which was asked shows.
    assert behind != ahead
    assert repo.git("merge-base", "HEAD", "main") == ahead
    assert test_map.merge_base(repo.path) == behind

    local_only = scratch()
    base = local_only.commit("a")
    local_only.git("branch", "main", base)
    work = local_only.commit("work")
    assert work != base
    assert test_map.merge_base(local_only.path) == base


def test_a_main_with_no_common_ancestor_is_passed_over(scratch):
    """An `origin/main` that exists and shares no commit with HEAD (a
    hand-made shallow graft) is passed over for `main`, as a ref that is
    not there is. Kills: the fall-through turned into a raise."""
    repo = scratch()
    base = repo.commit("a")
    repo.git("branch", "main", base)
    repo.git("checkout", "-q", "--orphan", "island")
    island = repo.commit("island")
    repo.git("update-ref", "refs/remotes/origin/main", island)
    repo.git("checkout", "-q", "trunk")
    repo.commit("work")
    assert island != base
    assert repo.git("rev-parse", "origin/main") == island
    assert not _shares_history(repo.path, "origin/main")
    assert test_map.merge_base(repo.path) == base


@pytest.mark.parametrize("shape", ["neither ref", "no common ancestor"])
def test_with_no_base_to_find_select_says_what_to_pass(scratch, shape):
    """The release rehearsal's checkout has neither ref; an island branch
    has both and shares history with neither. Each is refused by an exit
    that names the argument to pass. Kills: the refusal turned into
    git's `CalledProcessError`; the message losing either phrase."""
    repo = scratch()
    base = repo.commit("a")
    if shape == "no common ancestor":
        repo.git("branch", "main", base)
        repo.git("update-ref", "refs/remotes/origin/main", base)
        repo.git("checkout", "-q", "--orphan", "alone")
        alone = repo.commit("alone")
        assert alone != base
        # The refs are there: it is the history that is missing.
        assert repo.git("rev-parse", "main") == base
        assert repo.git("rev-parse", "origin/main") == base
    for ref in ("origin/main", "main"):
        assert not _shares_history(repo.path, ref)
    assert _is_the_refusal(_refusal(repo))


def test_a_named_base_is_never_replaced_by_main(scratch):
    """`--changed-base=release/9.9` that does not resolve is refused, with
    a `main` right there to fall back to. Kills: the named base tried
    first and the default list after it."""
    repo = scratch()
    base = repo.commit("a")
    repo.git("branch", "main", base)
    repo.git("update-ref", "refs/remotes/origin/main", base)
    release = repo.commit("released")
    repo.git("branch", "release/1.0", release)
    repo.commit("fix")
    assert test_map.merge_base(repo.path) == base
    assert _is_the_refusal(_refusal(repo, "release/9.9"))
    assert release != base
    assert test_map.merge_base(repo.path, "release/1.0") == release


def test_the_guard_asks_for_a_merge_base_not_for_the_ref(scratch):
    """`test_select_prints_its_reasons_and_what_it_is_for` takes the
    default path only where it can succeed. Kills: the guard asking
    `git rev-parse --verify`, under which a clone holding an unrelated
    `origin/main` takes the default path and fails on a correct refusal."""
    repo = scratch()
    base = repo.commit("a")
    repo.git("branch", "main", base)
    assert _shares_history(repo.path, "main")
    assert not _shares_history(repo.path, "origin/main")   # no such ref
    repo.git("update-ref", "refs/remotes/origin/main", base)
    repo.git("checkout", "-q", "--orphan", "alone")
    repo.commit("alone")
    assert repo.git("rev-parse", "--verify", "--quiet", "origin/main") == base
    assert not _shares_history(repo.path, "origin/main")
    assert not _shares_history(repo.path, "main")


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


@pytest.mark.parametrize("suite_status", [0, 1])
def test_a_build_whose_combine_left_a_data_file_writes_no_map(
        tmp_path, monkeypatch, capsys, suite_status):
    """#975: `coverage combine` exits 0 over a data file it cannot read.

    At the 1.0.0rc14 cut it said `Combined 992 files, skipped 24157, 1
    file errored`, exited 0, and `build` wrote a map lacking whatever
    that worker ran: a map that under-selects, with nothing in it or in
    `pytest --changed` to say so. The file it could not read is the one
    it leaves behind, so `build` refuses on that: no map is written, a
    map already there is left as it was, and the exit is non-zero even
    when the suite was green. The suite's own status is printed, and is
    what a red suite still exits with: this run is also the release's
    3.14t integration run, and its result stands.
    """
    left = ".coverage.host.pid7.Xunread"

    class Done:
        def __init__(self, returncode, stdout=""):
            self.returncode, self.stdout = returncode, stdout

    def run(cmd, **kwargs):
        if "--collect-only" in cmd:
            return Done(0, "tests/test_x.py::test_a\n")
        if "combine" in cmd:
            data = Path(kwargs["env"]["COVERAGE_FILE"])
            assert data.name == ".coverage"
            (data.parent / left).write_bytes(b"not a database")
            return Done(0)
        return Done(suite_status if "run" in cmd else 0)

    def never(*args, **kwargs):
        raise AssertionError("a map was read out of data combine did not "
                             "finish reading")

    monkeypatch.setattr(test_map.subprocess, "run", run)
    monkeypatch.setattr(test_map, "from_coverage", never)
    previous = tmp_path / test_map.MAP_FILE
    previous.write_text("the map of the release before")

    with pytest.raises(SystemExit) as stopped:
        test_map.main(["build", "--sha", "abc", "--out", str(tmp_path)])

    # The literal, not the constant: 1 is pytest's "tests failed", and a
    # constant set to 1 would turn a green integration run into a red one
    # in the release record with every other assertion here still true.
    assert test_map.EXIT_NO_MAP == 9
    assert stopped.value.code == (suite_status or 9)
    assert previous.read_text() == "the map of the release before"
    said = capsys.readouterr().out
    assert left in said
    assert f"the suite exited {suite_status}" in said
    assert "no map" in said


class _Ran:
    def __init__(self, returncode, stdout=""):
        self.returncode, self.stdout = returncode, stdout


def _build_with_combine(monkeypatch, tmp_path, suite_status, combine_step):
    """Run `build` through its command line with doubles for the three
    commands it launches. `combine_step(data)` is handed the combined
    file's path and returns combine's exit status. The double honours
    `check=True` as `subprocess.run` does, so a build that checks a
    command which exited non-zero dies here as it would for real."""
    def run(cmd, **kwargs):
        if "--collect-only" in cmd:
            done = _Ran(0, "tests/test_x.py::test_a\n")
        elif "combine" in cmd:
            done = _Ran(combine_step(Path(kwargs["env"]["COVERAGE_FILE"])))
        else:
            done = _Ran(suite_status if "run" in cmd else 0)
        if kwargs.get("check") and done.returncode:
            raise subprocess.CalledProcessError(done.returncode, cmd)
        return done

    monkeypatch.setattr(test_map.subprocess, "run", run)
    with pytest.raises(SystemExit) as stopped:
        test_map.main(["build", "--sha", "abc", "--out", str(tmp_path)])
    return stopped.value.code


def _no_map_from_unread_data(*args, **kwargs):
    raise AssertionError("a map was read out of data combine did not "
                         "finish reading")


@pytest.mark.parametrize("suite_status", [0, 1])
def test_a_build_whose_combine_failed_leaving_its_files_writes_no_map(
        tmp_path, monkeypatch, capsys, suite_status):
    """Review of #1007: `coverage combine` exits 1, not 0, over a data
    file with a table missing (`no such table: other_db.context`, the
    half-written shape `.coveragerc` names), and leaves every data file.
    `build` checked that command, so it died with a traceback and exit 1
    and never printed the suite's status: a green 3.14t integration run
    read as pytest's "tests failed". It is the same refusal as a file
    left behind, with the same exit."""
    left = [".coverage.host.pid7.Xa", ".coverage.host.pid8.Xb"]

    def combine_step(data):
        for name in left:
            (data.parent / name).write_bytes(b"a table short")
        return 1

    monkeypatch.setattr(test_map, "from_coverage", _no_map_from_unread_data)
    previous = tmp_path / test_map.MAP_FILE
    previous.write_text("the map of the release before")

    code = _build_with_combine(monkeypatch, tmp_path, suite_status,
                               combine_step)

    assert code == (suite_status or 9)
    assert previous.read_text() == "the map of the release before"
    said = capsys.readouterr().out
    assert f"the suite exited {suite_status}" in said
    assert "no map written" in said
    assert "coverage combine exited 1" in said
    assert all(name in said for name in left)


@pytest.mark.parametrize("suite_status", [0, 1])
def test_a_build_whose_combine_failed_leaving_nothing_writes_no_map(
        tmp_path, monkeypatch, capsys, suite_status):
    """`coverage combine` also exits 1 with `No data to combine`, when no
    process wrote a data file. Nothing is left to name, so the message
    says that; the suite's status and the exit are as for any build that
    wrote no map."""
    monkeypatch.setattr(test_map, "from_coverage", _no_map_from_unread_data)
    previous = tmp_path / test_map.MAP_FILE
    previous.write_text("the map of the release before")

    code = _build_with_combine(monkeypatch, tmp_path, suite_status,
                               lambda data: 1)

    assert code == (suite_status or 9)
    assert previous.read_text() == "the map of the release before"
    said = capsys.readouterr().out
    assert f"the suite exited {suite_status}" in said
    assert "no map written" in said
    assert "coverage combine exited 1 and left no data file" in said


def test_a_build_whose_combine_read_everything_writes_the_map(
        tmp_path, monkeypatch, capsys):
    """The other direction, and it needs no `coverage`, so a runner sees
    it: the combined file combine writes is not a file it could not read.
    A leftover check that matched it would refuse every map, and only the
    cases below, which skip where `coverage` is not installed, said so."""
    def combine_step(data):
        data.write_bytes(b"the combined data")
        return 0

    monkeypatch.setattr(test_map, "from_coverage",
                        lambda *a, **k: {"functions": {}, "workers": {},
                                         "unmapped": []})

    code = _build_with_combine(monkeypatch, tmp_path, 0, combine_step)

    assert code == 0
    assert (tmp_path / test_map.MAP_FILE).exists()
    assert "no map" not in capsys.readouterr().out


def _coverage_child_env(data_file):
    """A child that runs coverage must not inherit conftest's COVERAGE_FILE."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("COVERAGE_")}
    env["COVERAGE_FILE"] = str(data_file)
    return env


@pytest.mark.parametrize("damage", ["truncated", "not sqlite", "none"])
def test_combine_names_the_data_file_it_could_not_read(tmp_path, damage):
    """The premise of the refusal above, against coverage itself (#975).

    Two processes each write a data file; one file is then damaged the
    way a loaded machine left one at the rc14 cut (`database disk image
    is malformed`). `coverage combine` still exits 0, and the damaged
    file is the one it does not delete. If a coverage release starts
    deleting it, or exiting non-zero, this is red and `build`'s check is
    what to look at.
    """
    try:
        import coverage  # noqa: F401
    except ImportError:
        pytest.skip("coverage is in the dev extra")
    (tmp_path / "one.py").write_text("x = 1\n")
    (tmp_path / "two.py").write_text("y = 2\n")
    rc = tmp_path / "rc"
    rc.write_text("[run]\nparallel = True\n")
    env = _coverage_child_env(tmp_path / ".coverage")
    for script in ("one.py", "two.py"):
        subprocess.run([sys.executable, "-m", "coverage", "run",
                        f"--rcfile={rc}", script],
                       cwd=tmp_path, env=env, check=True, timeout=120)
    files = sorted(tmp_path.glob(".coverage.*"))
    assert len(files) == 2, files
    victim = files[0]
    whole = victim.read_bytes()
    if damage == "truncated":
        victim.write_bytes(whole[:len(whole) // 2])
    elif damage == "not sqlite":
        victim.write_bytes(b"\x00" * 4096)

    status, unread = test_map.combine(rc, env, tmp_path)

    assert status == 0
    assert unread == ([] if damage == "none" else [victim.name])
    assert (tmp_path / ".coverage").exists()


def test_a_map_whose_commit_is_not_here_is_no_map(tmp_path):
    (tmp_path / test_map.MAP_FILE).write_text(
        '{"sha": "0000000000000000000000000000000000000000", "python": "x", '
        '"functions": {}, "workers": {}, "unmapped": []}')
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    mapping, why = test_map.load(tmp_path)
    assert mapping is None and "does not have" in why


@pytest.mark.parametrize("text, said", [
    ('{"sha": "abc", "funct', "does not parse"),
    ("\xff\xfe", "does not parse"),
    ('{"sha": "abc"}', "lacks the keys"),
    ("[]", "lacks the keys")])
def test_a_corrupt_map_is_no_map_and_says_why(tmp_path, text, said):
    # A truncated map failed the run with an INTERNALERROR (#734 review,
    # finding 4); unusable is the no-map fallback, with its reason.
    (tmp_path / test_map.MAP_FILE).write_bytes(text.encode("latin-1"))
    mapping, why = test_map.load(tmp_path)
    assert mapping is None and said in why
    sel = _select([C("isocenter/session.py", "DicomSession.compact")],
                  mapping=None, no_map=why)
    assert sel.files == {"tests/test_session.py", "tests/test_new.py"}
    assert said in sel.reasons[0]


@pytest.mark.parametrize("base_args", [
    ["--changed-base=main"], ["--changed-base", "main"],
    # A path that is the whole suite: `args_source` said ARGS and the
    # check was skipped (#734 review, finding 5).
    ["--changed-base=main", "tests"]])
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
    # The tests that read every `*.py` by glob come along with any
    # package edit (#734 review), and so do the ones that read this
    # module's source by name (#779); what rule 1 chose is the rest.
    by_glob = test_map.glob_readers(proj, "isocenter/session.py")
    by_name = test_map.source_readers(proj, "isocenter/session.py")
    assert by_glob and by_name - by_glob
    assert "tests/test_compaction.py" not in by_glob | by_name
    assert by_glob | by_name <= sel.files
    assert _files(sel) - by_glob - by_name == {"tests/test_compaction.py"}


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

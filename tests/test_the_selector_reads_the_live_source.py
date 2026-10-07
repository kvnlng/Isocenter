"""`pytest --changed`'s assumptions about the package, checked live (#707).

Kept apart from tests/test_changed_code_selects_its_tests.py on purpose.
These read `isocenter/**/*.py` by glob, so every package edit selects the
file that holds them (`glob_readers`, #734 review). That file takes about
a hundred seconds, most of it the small-real-map fixture, and would ride
along with every package edit if these lived there.
"""
import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# By path and appended, never inserted at 0: a future scripts/<name>.py
# must not be able to shadow a stdlib module inside the pytest process.
sys.path.append(str(REPO / "scripts"))
import test_map  # noqa: E402


def test_the_package_has_no_one_line_function():
    # Why a one-line def's worker filing is accepted rather than handled
    # (`split_functions`; the counterexample is in the selector's tests).
    one_line = [
        f"{path.relative_to(REPO)}::{name}"
        for path in sorted((REPO / "isocenter").rglob("*.py"))
        for name, _def, _end, body in test_map.functions_in(
            path.read_text(encoding="utf-8"))
        if body == _def]
    assert one_line == [], (
        "a one-line def is filed as worker-run whenever a spawned process "
        "imports its module; see split_functions before adding one")


def test_the_dispatch_finder_sees_every_worker_in_the_live_source():
    found = test_map.dispatched_workers(REPO)
    assert {"scan_worker", "_verify_worker", "_discover_worker",
            "ingest_worker", "_export_instance_worker",
            "execute_redaction_task"} <= found, (
        "a worker function has no hand-off the finder recognises, so an "
        "edit to it would fall past rule 2")
    assert all(name for handoffs in test_map.dispatchers(REPO).values()
               for _path, name in handoffs), (
        "a worker is handed to a pool at module scope; rule 2 cannot "
        "reach its tests and widens to the row instead -- decide whether "
        "that is wanted before accepting it")
    # Rule 2 pairs a worker with its dispatchers by the last dotted part
    # alone, so a handed name must be the last part of one function only:
    # a `pool.submit(self.run)` would make every `*.run` ask that one
    # dispatcher, merged silently in `dispatchers()` (#744, review of #734).
    # Two keys are not package functions at all: `func`, the parameter
    # parallel._run_on_new_executor hands on, and `os.getpid`, submitted
    # by io_handlers._ingest_results. No package function may take either
    # name, or an edit to it would ask those dispatchers (review of #778).
    # Any other key names exactly one: a new key that names none is a new
    # hand-off of something outside the package, to be added here.
    not_package = {"func", "getpid"}
    defined = {}
    for path in sorted((REPO / "isocenter").rglob("*.py")):
        for qualname, *_ in test_map.functions_in(path.read_text(encoding="utf-8")):
            defined.setdefault(qualname.rsplit(".", 1)[-1], []).append(
                f"{path.relative_to(REPO).as_posix()}::{qualname}")
    assert not_package <= found, (
        "a key named here is no longer handed to a pool; drop it")
    wrong = {name: defined.get(name, [])
             for name in found
             if len(defined.get(name, ())) != (0 if name in not_package else 1)}
    assert wrong == {}, (
        "a handed worker's last name part is not the last part of exactly "
        "one package function (or, for a name that is not a package "
        "function, of none); key `dispatchers()` on more of the name "
        "(see the comment in select())")


def test_every_pool_call_in_the_package_resolves_to_a_dispatcher():
    """Asked of the calls, not of a list of names (#734 review, finding 1).

    Found independently of `pool_calls`: every call whose callee's last
    part is `run_parallel`, a pool method called on something, or a pool
    maker. The finder once looked only for a bare argument ending
    `_worker` and missed `redact()`'s
    `run_parallel(service.execute_redaction_task, ...)`.
    """
    calls = set()
    for path in sorted((REPO / "isocenter").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        rel = path.relative_to(REPO).as_posix()
        for node in ast.walk(ast.parse(source)):
            func = getattr(node, "func", None)
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if isinstance(node, ast.Call) and (
                    name in test_map.HAND_OFFS + test_map.POOL_MAKERS
                    or (isinstance(func, ast.Attribute)
                        and name in test_map.POOL_METHODS)):
                calls.add((rel, test_map.function_at(source, node.lineno)))
    assert ("isocenter/session.py", "DicomSession._apply_redaction_rules") in calls
    known = set().union(*test_map.dispatchers(REPO).values())
    assert calls - known == set(), "a pool call no dispatcher stands for"


def test_the_glob_detector_finds_the_tests_that_read_docs_and_source():
    # The tests the #734 review found reading by glob, and the two
    # source-text tests no TARGETS row holds (finding 7).
    md = test_map.glob_readers(REPO, "docs/" + "nobody-names-this." + "md")
    assert {"tests/test_doc_anchors.py", "tests/test_documented_api_exists.py",
            "tests/test_documented_output_matches.py",
            "tests/test_documented_zones_are_zone_space.py",
            "tests/test_source_citations.py"} <= md
    py = test_map.glob_readers(REPO, "isocenter/" + "remediation" + ".py")
    assert {"tests/test_source_citations.py",
            "tests/test_documented_env_vars.py",
            "tests/test_the_selector_reads_the_live_source.py"} <= py
    # Walks the package with os.walk and keeps what ends with the Python
    # suffix: no glob literal, and an `Equipment()` added to privacy.py
    # selected 2909 tests but not this one, which failed on the edit (#744).
    assert "tests/test_api_coherence.py" in py
    assert "tests/test_doc_anchors.py" not in py
    assert "tests/test_changed_code_selects_its_tests.py" not in py, (
        "the selector's slow test file reads the package by glob, so every "
        "package edit now selects it; keep live-source checks in this file")


# --- a test that reads one named module's source (#779) -------------------
#
# Kept in this file because the first two read the live suite, and this
# file is already selected by every package edit. The synthetic sources
# below are spelled in pieces where a whole spelling would make this file
# say something about itself that the assertions then read back.

C = test_map.Change
#: The two tests of the issue: (module, a function in it, the test file
#: that reads the module's source and runs none of that function).
ISSUE_779 = [
    ("isocenter/pixel_geometry.py", "_contradiction",
     "tests/test_pixel_geometry.py"),
    ("isocenter/exporters/wfdb.py", "WfdbExporter.export",
     "tests/test_wfdb_privacy.py"),
]


def _a_function_edit(path, qualname, **kw):
    """`select()` over the live suite for one function a map covers.

    The map is made here and `unspoken` is empty, on purpose: a stale map
    in this checkout selects both files by the widening for tests it
    cannot speak for, and the test would pass with the detector deleted.
    """
    mapping = {"functions": {path: {qualname: ["tests/test_ran_it.py::test_x"]}},
               "workers": {}, "unmapped": []}
    return test_map.select(mapping, {C(path, qualname)}, [], {}, REPO,
                           dispatching={}, unspoken=frozenset(),
                           wide=lambda test_file: False, **kw)


def test_a_function_edit_selects_the_tests_that_read_its_modules_source():
    """Kills: the detector asked only of paths outside any function (rule
    7's place), or not asked at all."""
    for path, qualname, reader in ISSUE_779:
        sel = _a_function_edit(path, qualname)
        assert not sel.full
        # The map was consulted: this is a function edit, not a fallback.
        assert sel.nodeids == {"tests/test_ran_it.py::test_x"}
        assert reader in sel.files, (path, sel.reasons)
        assert any(reason.startswith(f"{path}: ") and reason.endswith(
            "test files read its source by name -> added")
            for reason in sel.reasons), sel.reasons
        # And it is this detector that brought it: nothing else selects
        # the file for this change.
        without = _a_function_edit(path, qualname, by_name=lambda p: set())
        assert reader not in without.files
        assert not any("by name" in reason for reason in without.reasons)


def _suite(tmp_path, **sources):
    (tmp_path / "tests").mkdir()
    for name, text in sources.items():
        (tmp_path / "tests" / f"test_{name}.py").write_text(text, encoding="utf-8")
    return tmp_path


def _readers(repo, path):
    return {Path(rel).stem[len("test_"):]
            for rel in test_map.source_readers(repo, path)}


def test_the_stem_alone_is_not_a_reader(tmp_path):
    """Rule 7's needle (the stem anywhere) over a function edit is the
    whole suite for `session.py` and five more. Kills: the source-reading
    spelling no longer required beside the word."""
    spelling = "inspect.get" + "source(thing)"
    repo = _suite(tmp_path,
                  says="session = Session('x.db')\nsession.audit()\n",
                  reads_another=f"import session_helpers\n{spelling}\n",
                  reads=f"from isocenter import session\n{spelling}\n")
    assert _readers(repo, "isocenter/session.py") == {"reads"}

    # The live suite: the readers of the module most files mention are a
    # few, not everything, and not nothing.
    live = test_map.source_readers(REPO, "isocenter/session.py")
    every = test_map.SuiteIndex(REPO).texts()
    assert 0 < len(live) < 60 < len(every), (len(live), len(every))
    assert "tests/test_the_selector_reads_the_live_source.py" in live


def test_the_stem_is_a_word_not_a_substring(tmp_path):
    """Kills: `in` for a word boundary."""
    spelling = "ast.par" + "se(text)"
    repo = _suite(tmp_path,
                  manager=f"from isocenter import persistence_manager\n{spelling}\n",
                  store=f"from isocenter import persistence\n{spelling}\n")
    assert _readers(repo, "isocenter/persistence.py") == {"store"}
    assert _readers(repo, "isocenter/persistence_manager.py") == {"manager"}


def test_a_basename_in_a_test_is_a_reader_with_no_ast_at_all(tmp_path):
    """A reader spelled `(ROOT / "isocenter" / "exporters" / "x.py")
    .read_text()` and a regular expression has none of the spellings.
    Kills: the file-name half dropped."""
    name = "wfdb" + ".py"
    repo = _suite(tmp_path,
                  opens=f'TEXT = (ROOT / "exporters" / "{name}").read_text()\n',
                  mentions="import wfdb\nrecord = wfdb.rdrecord('r')\n")
    assert _readers(repo, "isocenter/exporters/wfdb.py") == {"opens"}
    assert _readers(repo, "isocenter/exporters/dicom.py") == set()


def test_each_source_reading_spelling_counts_and_only_a_module_has_readers(tmp_path):
    """Kills: a spelling dropped from the list; a page or a script given
    source readers (rule 7 already names those)."""
    spellings = {"getsource": "inspect.get" + "source(m)",
                 "getsourcelines": "inspect.get" + "sourcelines(m)",
                 "parse": "ast.par" + "se(t)",
                 "file": "Path(uids._" + "_file__)"}
    repo = _suite(tmp_path, own_file="HERE = Path(_" + "_file__)\nuids = 1\n",
                  **{key: f"from isocenter import uids\n{text}\n"
                     for key, text in spellings.items()})
    # `own_file` holds the word and its own path, with no dot before it:
    # nearly every test file does, and it reads nobody's source.
    assert _readers(repo, "isocenter/uids.py") == set(spellings)
    assert test_map.source_readers(repo, "docs/uids.md") == set()
    assert test_map.source_readers(repo, "scripts/uids.py") == set()

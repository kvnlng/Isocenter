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
    defined = {}
    for path in sorted((REPO / "isocenter").rglob("*.py")):
        for qualname, *_ in test_map.functions_in(path.read_text(encoding="utf-8")):
            defined.setdefault(qualname.rsplit(".", 1)[-1], []).append(
                f"{path.relative_to(REPO).as_posix()}::{qualname}")
    shared = {name: defined[name] for name in found
              if len(defined.get(name, ())) > 1}
    assert shared == {}, (
        "two functions share a handed worker's last name part; key "
        "`dispatchers()` on more of the name (see the comment in select())")


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

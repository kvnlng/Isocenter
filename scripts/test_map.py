"""Which tests exercise which functions, and what a change selects (#707).

    python -m scripts.test_map build     # 3.14t, clean tree at a known SHA
    python -m scripts.test_map select    # print what --changed would run

Since 2026-09-17 the selection is the pre-merge check (RELEASING.md,
step 3); the full suite runs when a release is cut. A test this misses
lets a regression onto `main`, so every doubt widens: no record -> the
module's TARGETS row -> the suite, and whatever the map cannot speak for
is added back from the touched modules' rows (`cannot_speak_for`).

The map is generated, gitignored and never edited. `TARGETS` in
scripts/mutation_probe.py stays the one maintained module-to-tests map;
this only narrows inside it.

Keyed by function name, not line number, because nothing rebuilds it on
every push. **An old map is not merely less sharp: it is ignorant** of
tests added since, of tests that skipped in the build, and of what a
function calls now that its body has changed. Those are computed from
git at selection time and widened within the touched modules' TARGETS
rows, so an old map selects more, toward those rows. It still misses a
call path added *across* modules since the build (`b.k()` now calls
`a.g()`; an edit to `g` touches only `a.py`, whose row does not hold
`test_k`), which is rule 3's own bound and the release integration run's
to find (spec §10 item 13).

Contexts are nodeids, switched by hooks in tests/conftest.py: "<startup>"
for everything outside a test, the nodeid around each test's whole
protocol, so what a fixture runs is attributed to the test. What is left
under the empty context ran in a spawned process, where no hook reaches.

Built on 3.14t with `concurrency = multiprocessing,thread`, because
coverage over spawned workers is what is slow on 3.12 -- 5.96 s bare
against 68.3 s for tests/test_multiprocessing.py + tests/test_crypto.py,
contexts or not -- where the free-threaded build pays 0.64 s against
1.00 s, and because `.coveragerc`'s `multiprocessing` alone replaces
coverage's default and leaves worker threads untraced (spec §10).
"""
import ast
import json
import os
import re
import subprocess
from collections import namedtuple
from dataclasses import dataclass, field
from pathlib import Path

MAP_FILE = ".test-map.json"
PURPOSE = ("this selection is the pre-merge check (RELEASING.md step 3): run "
           "it on 3.12 and 3.14t; the full suite runs when a release is cut")
FULL_SUITE_PATHS = ("tests/conftest.py", "tests/support/", "setup.py",
                    "pytest.ini", ".coveragerc", "pyproject.toml",
                    "MANIFEST.in")
Change = namedtuple("Change", "path qualname")


def context_to_nodeid(context):
    return context.split("[", 1)[0]


def functions_in(source):
    """(qualname, def line, last line, first body line), outermost first."""
    found = []

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef)):
                name = prefix + child.name
                if not isinstance(child, ast.ClassDef):
                    found.append((name, child.lineno, child.end_lineno,
                                  child.body[0].lineno))
                walk(child, name + ".")
            else:
                walk(child, prefix)

    walk(ast.parse(source), "")
    return found


def _span_at(spans, line):
    best = None
    for span in spans:
        if span[1] <= line <= span[2] and (best is None or span[1] >= best[1]):
            best = span
    return best


def function_at(source, line):
    """The innermost function holding `line`, or None."""
    span = _span_at(functions_in(source), line)
    return span[0] if span else None


def split_functions(source, contexts_by_lineno):
    """({function: [tests]}, [functions a spawned worker ran])."""
    spans = functions_in(source)
    named, in_workers = {}, set()
    for lineno, contexts in contexts_by_lineno.items():
        span = _span_at(spans, lineno)
        # The signature -- the `def` line and every default-argument line
        # under it -- runs when the module or class body does, not when
        # the function is called. Counting it would file every function
        # that was never called as "ran in a worker". A one-line
        # `def f(): return 1` has no line to tell the two apart, so it is
        # counted: a spawned process importing it files it under workers,
        # and an edit to it then selects the dispatchers' tests (rule 2)
        # rather than its module's row (rule 3) -- a different set, not a
        # strictly larger one. There are none in isocenter/ (2026-09-17).
        if span is None or lineno < span[3]:
            continue
        for context in contexts:
            if context == "":
                in_workers.add(span[0])
            elif not context.startswith("<"):
                named.setdefault(span[0], set()).add(context_to_nodeid(context))
    return ({name: sorted(tests) for name, tests in sorted(named.items())},
            sorted(in_workers))


def from_coverage(data_path, repo, sha, python, collected=()):
    import coverage
    data = coverage.CoverageData(str(data_path))
    data.read()
    repo = Path(repo).resolve()
    result = {"sha": sha, "python": python, "functions": {}, "workers": {}}
    for measured in sorted(data.measured_files()):
        try:
            rel = Path(measured).resolve().relative_to(repo).as_posix()
        except ValueError:
            continue
        functions, workers = split_functions(
            Path(measured).read_text(encoding="utf-8"),
            data.contexts_by_lineno(measured))
        if functions:
            result["functions"][rel] = functions
        if workers:
            result["workers"][rel] = workers
    ran = {node for per_file in result["functions"].values()
           for tests in per_file.values() for node in tests}
    # Collected but never seen running anything in the package: skipped on
    # this interpreter, or a test of the repo rather than of the code. The
    # map cannot speak for these, and says so.
    result["unmapped"] = sorted({context_to_nodeid(n) for n in collected} - ran)
    return result


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_hunks(diff_text):
    """({path: [old-side ranges]}, {path: [new-side ranges]}).

    A side with no lines -- the old side of a pure insertion, the new
    side of a pure deletion -- contributes nothing: a deleted function is
    found on the old side, under its own name, not by guessing from the
    line the deletion happens to sit after.
    """
    old, new, old_path, new_path = {}, {}, None, None
    in_header = False
    for line in diff_text.splitlines():
        # `---`/`+++` are headers only between `diff --git` and the first
        # hunk. A deleted source line reading `-- a/x` renders as
        # `--- a/x` inside a hunk and must not switch the path (measured).
        if line.startswith("diff --git "):
            in_header, old_path, new_path = True, None, None
        elif in_header and line.startswith("--- "):
            old_path = line[6:].strip() if line.startswith("--- a/") else None
        elif in_header and line.startswith("+++ "):
            new_path = line[6:].strip() if line.startswith("+++ b/") else None
        else:
            match = _HUNK.match(line)
            if not match:
                continue
            in_header = False
            for path, side, start, count in (
                    (old_path, old, match.group(1), match.group(2)),
                    (new_path, new, match.group(3), match.group(4))):
                count = 1 if count is None else int(count)
                if path and count:
                    side.setdefault(path, []).append(
                        (int(start), int(start) + count - 1))
    return old, new


def changes_in(path, source, ranges):
    try:
        spans = functions_in(source)
    except SyntaxError:
        # Mid-edit and unparseable: no function can be named, so the whole
        # module is "changed outside any function" and falls to its row.
        return {Change(path, None)}
    out = set()
    for start, end in ranges:
        for line in range(start, end + 1):
            span = _span_at(spans, line)
            out.add(Change(path, span[0] if span else None))
    return out


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout


# Whatever the developer's git config says: no a/ b/ prefix games, no
# rename pairing (a 100% rename has no hunk at all), no external differ.
_DIFF = ("-c", "diff.noprefix=false", "-c", "diff.mnemonicprefix=false",
         "diff", "--no-renames", "--no-ext-diff", "--no-color")


def _is_module(path):
    return path.startswith("isocenter/") and path.endswith(".py")


def changed(repo, old, new=None):
    """(function changes, every other changed path) between two trees.

    `new=None` is the working tree, untracked files included.
    """
    ends = [old] if new is None else [old, new]
    names = _git(repo, *_DIFF, "--name-only", "-z", *ends, "--", ".")
    paths = {p for p in names.split("\0") if p}
    if new is None:
        untracked = _git(repo, "ls-files", "--others", "--exclude-standard", "-z")
        paths |= {p for p in untracked.split("\0") if p}
    old_side, new_side = parse_hunks(_git(repo, *_DIFF, "-U0", *ends, "--", "."))
    changes = set()
    for path in paths:
        if not _is_module(path):
            continue
        if path in old_side:
            source = _git(repo, "show", f"{old}:{path}")
            changes |= changes_in(path, source, old_side[path])
        if path in new_side:
            source = (_git(repo, "show", f"{new}:{path}") if new
                      else (Path(repo) / path).read_text(encoding="utf-8"))
            changes |= changes_in(path, source, new_side[path])
    # A module with hunks is spoken for by its functions. One without --
    # added empty, deleted, mode-only, untracked -- stays in `other`.
    return changes, sorted(paths - {c.path for c in changes})


def merge_base(repo, upstream=None):
    for candidate in ([upstream] if upstream else ["origin/main", "main"]):
        try:
            return _git(repo, "merge-base", "HEAD", candidate).strip()
        except subprocess.CalledProcessError:
            continue
    raise SystemExit("--changed needs the branch this work will merge into; "
                     "pass --changed-base=release/X.Y for a patch")


@dataclass
class Selection:
    full: bool = False
    nodeids: set = field(default_factory=set)
    files: set = field(default_factory=set)
    reasons: list = field(default_factory=list)
    touched: set = field(default_factory=set)


def _worker_calls(repo):
    """(path, dispatching function, worker name) for every pool hand-off.

    A hand-off is a call one of whose positional arguments is a bare name
    ending `_worker`: `run_parallel(ingest_worker, batch, ...)`, or the
    export pool's call with `_export_instance_worker` on its own line. By
    convention rather than by list, so a sixth worker is found the day it
    is written.
    """
    for path in sorted((Path(repo) / "isocenter").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        rel = path.relative_to(repo).as_posix()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call):
                for arg in node.args:
                    if isinstance(arg, ast.Name) and arg.id.endswith("_worker"):
                        yield rel, function_at(source, node.lineno), arg.id


def dispatchers(repo):
    """{worker name: {(path, the function that hands it to a pool)}}.

    Per worker, so rule 2 can ask only the dispatchers of the worker that
    was edited (spec §10 item 17). `None` for the function is a hand-off
    at module scope.
    """
    out = {}
    for rel, name, worker in _worker_calls(repo):
        out.setdefault(worker, set()).add((rel, name))
    return out


def dispatched_workers(repo):
    return set(dispatchers(repo))


def _tests_naming(repo, path):
    # The basename, and the stem only for Python: `import test_map` names
    # scripts/test_map.py without its suffix, but the stem of
    # docs/session.md is a word half the suite contains. RELEASING.md
    # step 3 states the same needle; keep the two identical.
    needles = {Path(path).name}
    if path.endswith(".py"):
        needles.add(Path(path).stem)
    return {test.relative_to(repo).as_posix()
            for test in (Path(repo) / "tests").glob("test_*.py")
            if any(n in test.read_text(encoding="utf-8") for n in needles)}


def select(mapping, changes, other, targets, repo, dispatching=None,
           unspoken=frozenset()):
    """`unspoken`: test files the map cannot speak for (`cannot_speak_for`)."""
    sel = Selection()
    if mapping is None:
        sel.reasons.append(
            f"no usable map at {MAP_FILE}: falling back to TARGETS rows "
            "(build one with `python -m scripts.test_map build`)")
    functions = (mapping or {}).get("functions", {})
    workers = (mapping or {}).get("workers", {})
    if dispatching is None:
        dispatching = dispatchers(repo)

    def row(path, why):
        if path in targets:
            sel.files.update(targets[path][0])
            sel.reasons.append(f"{path}: {why} -> its TARGETS row")
        else:
            sel.full = True
            sel.reasons.append(
                f"{path}: {why} and no TARGETS row -> the full suite")

    for change in sorted(changes, key=lambda c: (c.path, c.qualname or "")):
        sel.touched.add(change.path)
        where = f"{change.path}::{change.qualname}"
        tests = set(functions.get(change.path, {}).get(change.qualname, ()))
        in_worker = change.qualname in workers.get(change.path, ())
        if change.qualname is None:
            row(change.path, "changed outside any function")
            continue
        if in_worker:
            # Not `elif`: a helper one unit test calls directly and forty
            # pool tests reach inside a worker is both.
            # A worker function itself is reached only through the calls
            # that hand *it* to a pool. Anything else a spawned process
            # ran -- a helper -- may sit under any worker, so it asks
            # every dispatcher. Asking every dispatcher for a worker too
            # left rule 2 dead for all five whenever one dispatcher had no
            # record (spec §10 item 17).
            own = dispatching.get(change.qualname)
            asked = own if own else set().union(*dispatching.values())
            via, blind = set(), []
            for path, name in sorted(asked, key=str):
                recorded = functions.get(path, {}).get(name, ()) if name else ()
                via.update(recorded)
                if not recorded:
                    blind.append(f"{path}::{name or '<module scope>'}")
            if blind:
                # One dispatcher with a record is not enough: if export's
                # has none, a helper edit would select the ingest and
                # audit tests and not one export test.
                row(change.path, f"{change.qualname} runs in workers and "
                                 f"{len(blind)} of its {len(asked)} "
                                 f"dispatcher(s) have no record "
                                 f"({blind[0]})")
            tests |= via
        if tests:
            sel.nodeids |= tests
            sel.reasons.append(
                f"{where}: {len(tests)} tests ran it"
                + (" or dispatch the pool it runs in" if in_worker else ""))
        elif not in_worker:
            row(change.path, f"{change.qualname} has no record")

    for path in other:
        if path.startswith(FULL_SUITE_PATHS):
            sel.full = True
            sel.reasons.append(f"{path}: shared test machinery -> the full suite")
        elif path.startswith("tests/test_") and path.endswith(".py"):
            sel.files.add(path)
            sel.reasons.append(f"{path}: a changed test file -> itself")
        elif _is_module(path):
            sel.touched.add(path)
            row(path, "added, deleted, or changed with no hunk")
        elif path.startswith("isocenter/"):
            sel.full = True
            sel.reasons.append(
                f"{path}: package data every session loads -> the full suite")
        else:
            named = _tests_naming(repo, path)
            if named:
                sel.files |= named
                sel.reasons.append(f"{path}: {len(named)} test files name it")
            elif path.startswith("docs/") or path.endswith(".md"):
                # Prose no test reads cannot break one. Without this every
                # dated spec costs the whole suite twice, which is the
                # per-PR full run the 2026-09-17 ruling ended.
                sel.reasons.append(
                    f"{path}: documentation no test names -> nothing")
            else:
                sel.full = True
                sel.reasons.append(
                    f"{path}: no test names it -> the full suite")

    rows = {f for path in sel.touched for f in targets.get(path, ([],))[0]}
    widened = (set(unspoken) & rows) - sel.files
    if widened:
        sel.files |= widened
        sel.reasons.append(
            f"{len(widened)} test files in the touched modules' rows are ones "
            "the map cannot speak for (new, skipped in the build, or running "
            "code that changed since it) -> added")
    return sel


def cannot_speak_for(mapping, repo, base):
    """Test files an old map is ignorant of, as of `base`.

    Three kinds, each an under-selection if left out: tests that did not
    run in the build (`unmapped`); test files changed since the build;
    and tests recorded against a function whose body has changed since --
    what they reach now is not what the map saw.
    """
    files = {node.split("::")[0] for node in mapping.get("unmapped", ())}
    moved, other = changed(repo, mapping["sha"], base)
    files |= {p for p in other if p.startswith("tests/test_")}
    ahead, other = changed(repo, base)
    files |= {p for p in other if p.startswith("tests/test_")}
    for change in moved:
        tests = mapping["functions"].get(change.path, {}).get(change.qualname, ())
        files |= {node.split("::")[0] for node in tests}
    return files


def unmatched(sel, collected):
    """Selected tests that no longer exist: renamed or deleted since the build."""
    known = {context_to_nodeid(n) for n in collected}
    return {n for n in sel.nodeids if n not in known}


def load(repo):
    path = Path(repo) / MAP_FILE
    if not path.exists():
        return None
    mapping = json.loads(path.read_text(encoding="utf-8"))
    try:
        # A map built at a commit this clone does not have cannot be
        # aged: `cannot_speak_for` needs the diff from it.
        _git(repo, "cat-file", "-e", mapping["sha"] + "^{commit}")
    except subprocess.CalledProcessError:
        return None
    return mapping


def selection_for(repo, upstream=None):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "mutation_probe", Path(__file__).resolve().parent / "mutation_probe.py")
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    mapping = load(repo)
    base = merge_base(repo, upstream)
    changes, other = changed(repo, base)
    unspoken = cannot_speak_for(mapping, repo, base) if mapping else set()
    # TARGETS rides along: the conftest hook needs it for
    # `fall_back_for_missing`, and loading the probe twice is waste.
    return (select(mapping, changes, other, probe.TARGETS, repo,
                   unspoken=unspoken), mapping, probe.TARGETS)


def fall_back_for_missing(sel, missing, targets):
    """Selected tests that are gone: their modules fall to their rows."""
    if not missing:
        return
    sel.nodeids -= missing
    for path in sorted(sel.touched):
        if path in targets:
            sel.files.update(targets[path][0])
        else:
            sel.full = True
    sel.reasons.append(
        f"{len(missing)} selected tests no longer exist (renamed or deleted "
        "since the map was built) -> the touched modules' TARGETS rows")


def describe(sel, mapping, repo):
    if mapping:
        behind = _git(repo, "rev-list", "--count",
                      f"{mapping['sha']}..HEAD").strip()
        out = [f"map: built at {mapping['sha'][:9]} on {mapping['python']}, "
               f"{behind} commits behind HEAD"]
    else:
        out = ["map: none usable"]
    out += [f"  {reason}" for reason in sel.reasons] or ["  nothing changed"]
    out.append("selected: the full suite" if sel.full else
               f"selected: {len(sel.nodeids)} tests + {len(sel.files)} files")
    out.append(PURPOSE)
    return "\n".join(out)


def build(repo, out_dir, sha=None):
    """Run the suite under per-test contexts and write the map to out_dir."""
    import sys
    import tempfile
    repo = Path(repo).resolve()
    if sha is None:
        # Untracked files do not move a tracked function; modified ones do.
        if _git(repo, "status", "--porcelain", "--untracked-files=no").strip():
            raise SystemExit("build wants a clean tree, so the map describes "
                             "a commit and not an edit in progress")
        sha = _git(repo, "rev-parse", "HEAD").strip()
    with tempfile.TemporaryDirectory() as scratch:
        # `.coveragerc` plus `thread`: its `concurrency = multiprocessing`
        # replaces coverage's default, so worker threads -- what
        # run_parallel() uses on 3.14t -- go untraced. Measured with both:
        # scan_worker's body lands under its test's nodeid. A scratch copy,
        # because .coveragerc's SIGTERM comment was measured as it stands.
        rc = Path(scratch) / "coveragerc"
        text = (repo / ".coveragerc").read_text(encoding="utf-8")
        if text.count("\nconcurrency = multiprocessing\n") != 1:
            raise SystemExit(".coveragerc no longer has the one `concurrency = "
                             "multiprocessing` line build() rewrites")
        rc.write_text(text.replace(
            "\nconcurrency = multiprocessing\n",
            "\nconcurrency = multiprocessing,thread\n"), encoding="utf-8")
        env = dict(os.environ, COVERAGE_FILE=str(Path(scratch) / ".coverage"),
                   PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(repo),
                   TEST_MAP_CONTEXTS="1")  # turns conftest's labelling on
        listing = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q"],
            cwd=repo, env=env, capture_output=True, text=True, check=True)
        collected = [line for line in listing.stdout.splitlines() if "::" in line]
        subprocess.run([sys.executable, "-m", "coverage", "run",
                        f"--rcfile={rc}", "-m", "pytest", "-q"],
                       cwd=repo, env=env, check=False)
        subprocess.run([sys.executable, "-m", "coverage", "combine",
                        f"--rcfile={rc}"], cwd=repo, env=env, check=True)
        gil = getattr(sys, "_is_gil_enabled", lambda: True)()
        mapping = from_coverage(Path(scratch) / ".coverage", repo, sha,
                                sys.version.split()[0] + ("" if gil else "t"),
                                collected)
    target = Path(out_dir) / MAP_FILE
    target.write_text(json.dumps(mapping), encoding="utf-8")
    print(f"wrote {target}: {len(mapping['functions'])} files with tested "
          f"functions, {len(mapping['workers'])} with functions a worker ran, "
          f"{len(mapping['unmapped'])} tests it cannot speak for")


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(prog="python -m scripts.test_map")
    sub = parser.add_subparsers(dest="command", required=True)
    built = sub.add_parser("build")
    built.add_argument("--out", default=".",
                       help="directory to write .test-map.json into; when "
                            "building in a `git archive` copy, the main checkout")
    built.add_argument("--sha", default=None,
                       help="the commit this tree is, for a `git archive` "
                            "copy, which is not a git repository")
    chosen = sub.add_parser("select")
    chosen.add_argument("--base", default=None,
                        help="the branch this work merges into, when it is "
                             "not main (a patch onto release/X.Y)")
    args = parser.parse_args(argv)
    repo = Path(__file__).resolve().parent.parent
    if args.command == "build":
        build(repo, args.out, args.sha)
    else:
        sel, mapping, _targets = selection_for(repo, args.base)
        print(describe(sel, mapping, repo))
        for name in sorted(sel.nodeids | sel.files):
            print(name)


if __name__ == "__main__":
    main()

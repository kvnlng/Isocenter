"""Sampled mutation probe: does the suite notice when behaviour changes?

Coverage says a line ran. It does not say a test would have failed had
that line done something else. This asks the second question: mutate one
decision in a module, run only the tests that import it, and see whether
anything goes red. A mutant that survives is a change to production
behaviour the suite cannot see.

Run it against the de-identification core, where a test that cannot fail
is not an inconvenience but a leak nobody is watching for:

    python -m scripts.mutation_probe                 # default targets, each at its own budget
    python -m scripts.mutation_probe 10              # one budget for every module: the cheap pass
    python -m scripts.mutation_probe 30 isocenter/session.py tests/test_session.py

The default run costs about an hour, and most of it is `io_handlers.py`:
its test list is 56 files and a *surviving* mutant pays the whole list
(~165s) where a kill exits at the first red test (~45s). The positional
budget is the override for every module at once; nothing in CI runs this
script.

**Sampled, not exhaustive.** It walks mutation sites at a fixed stride,
so the output is "of N representative mutations, M survived" -- evidence
about whether the suite bites, not a mutation score to track over time.
Adding an operator renumbers the sites, so two runs across such a change
are not comparable sample-for-sample.

**The operator set decides what can be measured.** Three operators see
decisions -- comparison flips, `and`/`or`, boolean constants -- and three
see straight-line code: dropping a `not`, replacing a returned value with
None, and deleting a bare expression statement. That second group exists
because the first reported `crypto.py` as 0 sites and 0 survivors: 73
lines of key derivation, encrypt and decrypt with almost no branching,
so there was nothing for a comparison flip to find. A module with 0
sites is unmeasured, not clean, and the run says so rather than printing
a zero and letting it be read as a pass (#106).

Still unreached: argument swaps between same-typed parameters, string and
bytes constant mutation (salts, encodings, key-derivation parameters),
and exception-handler removal.

**A survivor is a question, not a verdict.** Some mutants are equivalent
-- they change the code without changing behaviour, and no test could
tell. Read each one before acting on it. The ones that matter are where
the mutated code is still plausible *and* the behaviour differs.

**The control run is load-bearing.** Before mutating, the module is
unparsed from its own AST and the tests are run against that. If the
control fails, the harness itself is perturbing the module and every
result below it is an artefact -- so it stops rather than reporting.

The mutated file is written in place and restored in a `finally`. Run it
on a clean tree so `git checkout isocenter/` is always a way out.

**A verdict is only about the mutation if the mutation was compiled.**
Runs carry `PYTHONDONTWRITEBYTECODE=1` and every write is checked against
CPython's own `.pyc` validation rule before the tests see it, because a
stale bytecode cache made this tool report mutations that were never in
the code it tested. `assert_fresh` has the mechanism (#174). The cache it
inspects is the one `PYTEST[0]` itself names, because the entry that
matters belongs to the interpreter that runs the tests, not the one that
launched the probe (#201).
"""

import ast, importlib.util, os, struct, subprocess, sys, time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYTEST = [str(REPO / ".venv/bin/python"), "-m", "pytest", "-x", "-q", "--no-header", "-p", "no:randomly"]

# The `(tests, budget)` to use for each target. Complete-ness of the
# test list matters more than it looks: a mutant that a test in this
# repo would kill, but which is not in that list, is reported as
# SURVIVED -- a phantom gap in the de-identification core that costs a
# human a real investigation.
# `tests/test_mutation_probe_targets.py` fails if a test file imports a
# target module and is not listed here. Extra entries are allowed but
# must have earned their runtime with a measured kill: every listed file
# runs for every sampled mutant. `test_remediation_actions.py` exercises
# `remediation.py` without importing it, which no import scan can see;
# `test_redaction_export.py` and `test_reversibility.py` do the same for
# `io_handlers.py` (the `apply_redaction_to_array` call and the
# `(0400,0510)` write, both verified kills).
# The budget is per module because the knob does two jobs at once
# globally: `io_handlers.py` has ~5x the sites of any other target, so
# raising its sampling density with one shared number forces the
# already-measured modules to re-pay at stride 1. The positional CLI
# budget overrides every module for one run.
TARGETS = {
    "isocenter/crypto.py": (["tests/test_crypto.py", "tests/test_reversibility.py"], 30),
    "isocenter/privacy.py": (["tests/test_analysis.py", "tests/test_analysis_persistence.py",
                              "tests/test_audit_suppression.py", "tests/test_automation.py",
                              "tests/test_config_tags_shapes.py", "tests/test_multiprocessing.py",
                              "tests/test_mutation_gaps.py", "tests/test_ocr_formal.py",
                              "tests/test_persistence.py", "tests/test_privacy.py",
                              "tests/test_private_sequence_implicit_vr.py",
                              "tests/test_profile_end_to_end.py", "tests/test_remediation.py",
                              "tests/test_remediation_actions.py",
                              "tests/test_remediation_invariants.py",
                              "tests/test_scaffold_features.py",
                              "tests/test_sr_anonymization.py"], 30),
    "isocenter/remediation.py": (["tests/test_audit_suppression.py", "tests/test_deid_tags.py",
                                  "tests/test_mutation_gaps.py", "tests/test_persistence.py",
                                  "tests/test_private_sequence_implicit_vr.py",
                                  "tests/test_remediation.py",
                                  "tests/test_remediation_accounting.py",
                                  "tests/test_remediation_actions.py",
                                  "tests/test_remediation_dates.py",
                                  "tests/test_remediation_invariants.py",
                                  "tests/test_phi_retention.py",
                                  "tests/test_scaffold_features.py"], 30),
    "isocenter/io_handlers.py": (["tests/test_api_coherence.py",
                                  "tests/test_audit_read_barrier.py",
                                  "tests/test_binary_retention_threshold.py",
                                  "tests/test_check_reversibility.py",
                                  "tests/test_codecs_strict.py",
                                  "tests/test_compress_handlers.py",
                                  "tests/test_compress_j2k_coverage.py",
                                  "tests/test_data_loss_reporting.py",
                                  "tests/test_export_atomic_write.py",
                                  "tests/test_export_contract.py",
                                  "tests/test_export_date_error.py",
                                  "tests/test_export_delivery_counters.py",
                                  "tests/test_export_error.py",
                                  "tests/test_export_failure_audit.py",
                                  "tests/test_export_loss_audit.py",
                                  "tests/test_export_merge_shape.py",
                                  "tests/test_export_pixels.py",
                                  "tests/test_export_readback.py",
                                  "tests/test_export_redaction_hash_warning.py",
                                  "tests/test_export_worker_graph_purity.py",
                                  "tests/test_float_pixel_data_export.py",
                                  "tests/test_ingest_failure_audit.py",
                                  "tests/test_io.py",
                                  "tests/test_legacy_waveform_hydration.py",
                                  "tests/test_logging.py",
                                  "tests/test_metadata_refactor_full.py",
                                  "tests/test_missing_study_date.py",
                                  "tests/test_multiprocessing.py",
                                  "tests/test_murmur_annotations.py",
                                  "tests/test_naming_structure.py",
                                  "tests/test_nested_phi_audit.py",
                                  "tests/test_pixel_geometry_pipeline.py",
                                  "tests/test_private_binary_ingest.py",
                                  "tests/test_private_tag_export.py",
                                  "tests/test_pydicom_deprecations.py",
                                  "tests/test_recursive_import.py",
                                  "tests/test_redaction_export.py",
                                  "tests/test_redaction_optimization.py",
                                  "tests/test_redaction_rgb.py",
                                  "tests/test_redaction_robustness.py",
                                  "tests/test_redaction_wildcard.py",
                                  "tests/test_remediation_accounting.py",
                                  "tests/test_reporting_features.py",
                                  "tests/test_reversibility.py",
                                  "tests/test_safe_export.py",
                                  "tests/test_services.py",
                                  "tests/test_session.py",
                                  "tests/test_shared_executor_lifecycle.py",
                                  "tests/test_sr_anonymization.py",
                                  "tests/test_structured_export.py",
                                  "tests/test_study_date_roundtrip.py",
                                  "tests/test_waveform_dicom_roundtrip.py",
                                  "tests/test_waveform_ingest.py",
                                  "tests/test_waveform_model.py",
                                  "tests/test_wfdb_conformance.py",
                                  "tests/test_wfdb_writer.py",
                                  "tests/test_worker_loss_is_reported.py"], 30),
}

class Mut(ast.NodeTransformer):
    """Applies exactly the nth mutation opportunity found."""
    def __init__(self, target): self.target, self.n, self.desc = target, 0, None
    def _hit(self):
        self.n += 1
        return self.n - 1 == self.target
    def visit_Compare(self, node):
        self.generic_visit(node)
        flip = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.GtE, ast.Gt: ast.LtE,
                ast.LtE: ast.Gt, ast.GtE: ast.Lt, ast.In: ast.NotIn, ast.NotIn: ast.In,
                ast.Is: ast.IsNot, ast.IsNot: ast.Is}
        if len(node.ops) == 1 and type(node.ops[0]) in flip:
            if self._hit():
                new = flip[type(node.ops[0])]
                self.desc = f"line {node.lineno}: {type(node.ops[0]).__name__} -> {new.__name__}"
                node.ops = [new()]
        return node
    def visit_BoolOp(self, node):
        self.generic_visit(node)
        if self._hit():
            new = ast.Or if isinstance(node.op, ast.And) else ast.And
            self.desc = f"line {node.lineno}: {type(node.op).__name__} -> {new.__name__}"
            node.op = new()
        return node
    def visit_Constant(self, node):
        if isinstance(node.value, bool):
            if self._hit():
                self.desc = f"line {node.lineno}: {node.value} -> {not node.value}"
                return ast.copy_location(ast.Constant(value=not node.value), node)
        return node

    # The three operators above only see *decisions*. A module of
    # straight-line calls has none, so it reported 0 sites and 0
    # survivors -- which reads like a clean bill of health next to
    # `privacy.py 11/36` and actually meant "not measured" (#106).
    # crypto.py, the reversible-anonymisation core, was in that state.
    # The three below reach code that decides nothing.
    def visit_UnaryOp(self, node):
        self.generic_visit(node)
        if isinstance(node.op, ast.Not) and self._hit():
            self.desc = f"line {node.lineno}: dropped `not`"
            return node.operand
        return node

    def visit_Return(self, node):
        self.generic_visit(node)
        already_none = (isinstance(node.value, ast.Constant)
                        and node.value.value is None)
        if node.value is not None and not already_none and self._hit():
            self.desc = f"line {node.lineno}: return <value> -> return None"
            return ast.copy_location(ast.Return(value=ast.Constant(value=None)), node)
        return node

    def visit_Expr(self, node):
        # A bare expression statement is there for its side effect, so
        # dropping it is the cheapest way to ask whether anything checks
        # that the side effect happened. Becomes `pass` rather than being
        # deleted: removing the only statement in a body leaves an AST
        # that will not unparse, and the failure would look like a
        # skipped mutant rather than a bug in this file.
        if isinstance(node.value, ast.Constant):
            return node  # a docstring; deleting it is an equivalent mutant
        self.generic_visit(node)
        if self._hit():
            self.desc = f"line {node.lineno}: deleted statement"
            return ast.copy_location(ast.Pass(), node)
        return node

def count_ops(src):
    n = 0
    while True:
        m = Mut(n); m.visit(ast.parse(src))
        if m.n <= n: return n
        n += 1

#: Answers from `subprocess_cache_path`, keyed on (interpreter, source
#: path). Per *path*, not per cache tag: the subprocess honours
#: `PYTHONPYCACHEPREFIX`/`sys.pycache_prefix`, so two sources under one
#: tag can cache into different directories and a tag-keyed memo would
#: hand one module the other's answer.
_SUBPROCESS_CACHE_PATHS = {}

def subprocess_cache_path(path):
    """The `__pycache__` entry `PYTEST[0]` would read for `path`.

    Asked of that interpreter, never derived here: a local
    `cache_from_source` names the file from the *parent's*
    `sys.implementation.cache_tag`, and the parent is routinely not the
    interpreter that runs the tests -- a pyenv shim beside the hardcoded
    `.venv` is the ordinary case, not the exotic one. Inspecting the
    parent-tag file degrades one direction only, never a false abort and
    always a false pass, so nothing about running the probe would ever
    say the guard had gone quiet (#201).

    The full path is requested rather than just the tag because the
    subprocess honours `PYTHONPYCACHEPREFIX`: a hand-assembled
    `<dir>/__pycache__/<stem>.<tag>.pyc` looks in the wrong directory
    under a cache prefix. One queried path covers every execution lever
    the run has -- `PYTEST[0]` is the process that imports the mutant,
    threads share it, and process workers spawn from the same
    `sys.executable` (so the same tag, with `PYTHONDONTWRITEBYTECODE`
    inherited either way).

    An interpreter that is missing or cannot answer is a `SystemExit`
    naming it, not a guess: the probe prefers aborting over reporting,
    and the raw FileNotFoundError this replaces was how a worktree
    without a `.venv` used to die mid-run.
    """
    resolved = Path(path).resolve()
    key = (PYTEST[0], str(resolved))
    if key not in _SUBPROCESS_CACHE_PATHS:
        query = ("import importlib.util, sys; "
                 "print(importlib.util.cache_from_source(sys.argv[1]))")
        try:
            r = subprocess.run([PYTEST[0], "-c", query, str(resolved)],
                               capture_output=True, text=True, timeout=60)
        except FileNotFoundError:
            raise SystemExit(
                f"ABORT: the test interpreter {PYTEST[0]} does not exist, so "
                f"no verdict it produced could be vouched for. Create the "
                f".venv (pip install -e '.[dev]') or point PYTEST at the "
                f"interpreter that should run the tests.") from None
        answer = r.stdout.strip()
        if r.returncode != 0 or not answer:
            raise SystemExit(
                f"ABORT: the test interpreter {PYTEST[0]} could not name its "
                f"bytecode cache for {resolved} (exit {r.returncode}: "
                f"{r.stderr.strip() or 'no output'}). A guard pointed at a "
                f"guessed path is #201's silent no-op again, so the probe "
                f"stops instead.")
        _SUBPROCESS_CACHE_PATHS[key] = Path(answer)
    return _SUBPROCESS_CACHE_PATHS[key]

def assert_fresh(path, cache):
    """Stop the run if a cached `.pyc` would be reused for what was just written.

    CPython validates a timestamp-based `.pyc` against the source's
    `(mtime, size)` pair, with the mtime truncated to whole seconds.
    Both halves collide far more easily here than they look.

    *Size* collides by construction. The probe writes `ast.unparse`
    output, so consecutive mutants differ from each other only by the
    mutation delta -- and two `Eq -> NotEq` flips, two dropped `not`s,
    or two `True -> False`s differ by exactly zero bytes. Comparison
    flips dominate most modules, so equal-size neighbours are the norm,
    not the exception.

    *Seconds* collide whenever a run is quick. `crypto.py`'s tests take
    0.6s, so consecutive writes land in the same second about half the
    time; the 15s runs on `privacy.py` are what makes this intermittent
    rather than constant.

    When both match, the interpreter hands back the *previous* mutant's
    bytecode and pytest never sees the mutation being scored. The verdict
    is then about code that was not there. It is not biased toward
    survivors either: it repeats the neighbour's verdict, so it invents
    a coverage gap or hides one depending on which way the neighbour
    went (#174).

    `PYTHONDONTWRITEBYTECODE=1` in `run()` is the fix -- not because it
    stops a `.pyc` being *read* (it does not) but because it stops each
    run planting the trap the next one falls into. A cache left behind by
    something else cannot spring it: its recorded mtime is in the past
    and the probe's writes are always later.

    This asserts that rather than trusting it, and aborts instead of
    printing a verdict. A probe that cannot tell "the suite did not
    notice" from "the suite was never shown" is exactly the silent
    failure it exists to hunt for.

    `cache` is required, with no default, on purpose. The cache that
    matters belongs to the interpreter that RUNS the tests -- callers
    pass `subprocess_cache_path(path)` -- and this function must never
    derive one itself: a `cache_from_source` fallback resurrects the
    parent interpreter's tag, and a pyenv shim launching the probe
    beside the hardcoded `.venv` is the routine case, not the edge. A
    guard built that way inspects a `.pyc` the subprocess never reads,
    finds nothing, and returns -- never a false abort, always a false
    pass, so no run would ever reveal the check had been off (#201).
    """
    cache = Path(cache)
    if not cache.exists():
        return
    head = cache.read_bytes()[:16]
    if len(head) < 16:
        return
    # Two deliberate divergences from `_validate_timestamp_pyc`: the
    # `& 0xFFFFFFFF` masking on both halves is omitted, which matters in
    # 2106 or on a 4GB source, and the magic number CPython checks first
    # is not, which matters only across a pre-release magic bump inside
    # one cache tag. Neither can reach this script.
    flags, mtime, size = struct.unpack("<III", head[4:16])
    if flags & 0b1:
        # Hash-based `.pyc`. CHECKED_HASH is verified against the source's
        # own hash and cannot go stale; UNCHECKED_HASH is trusted blind,
        # which is worse than the timestamp case, not better.
        if flags & 0b10:
            return
        why = "an UNCHECKED_HASH .pyc is reused without looking at the source"
    else:
        st = path.stat()
        if not (mtime == int(st.st_mtime) and size == st.st_size):
            return
        why = (f"its recorded (mtime={mtime}, size={size}) matches the file "
               f"just written")
    raise SystemExit(
        f"ABORT: {cache} is stale bytecode that CPython would reuse -- {why}. "
        f"pytest would execute the previous mutant and the verdict would not "
        f"be about this mutation. See assert_fresh() and #174.")

def run(tests):
    # PYTHONDONTWRITEBYTECODE rather than `-B`: `run_parallel()` spawns
    # worker processes, and a grandchild that writes a `.pyc` plants the
    # same trap the parent avoided. The variable is inherited
    # unconditionally; an interpreter flag reaches a spawned child only
    # through `_args_from_interpreter_flags`, which is not a promise this
    # script should rest on. `os.environ` is copied, not replaced -- a bare
    # `env=` dict loses PATH and the failure looks like a killed mutant.
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    r = subprocess.run(PYTEST + tests, cwd=REPO, capture_output=True, text=True,
                       timeout=900, env=env)
    return r.returncode == 0

def main():
    argv = sys.argv[1:]
    override = int(argv[0]) if argv and argv[0].isdigit() else None
    rest = argv[1:] if argv and argv[0].isdigit() else argv
    if len(rest) >= 2:
        targets = {rest[0]: (list(rest[1:]), override if override is not None else 30)}
    else:
        targets = TARGETS

    for mod, (tests, budget) in targets.items():
        if override is not None:
            budget = override
        path = REPO / mod
        original = path.read_text()
        total = count_ops(original)

        # Control: unparsed-but-unmutated must still pass, or every
        # result below is an artefact of the harness rather than a finding.
        path.write_text(ast.unparse(ast.parse(original)))
        try:
            assert_fresh(path, subprocess_cache_path(path))
            ok = run(tests)
        finally:
            path.write_text(original)
        print(f"\n### {mod}  ({total} mutation sites, sampling {budget})")
        print(f"    control (unparsed, unmutated): {'PASS' if ok else 'FAIL -- results unusable'}")
        if not ok:
            continue
        # A module with no sites was NOT MEASURED. Left to speak for
        # itself, "0 survived" sits in a table next to "11/36" and reads
        # as the healthiest row (#106).
        if total == 0:
            print("    => NOT MEASURED: no operator in this probe can see "
                  "this module. This is not a clean result -- it is the "
                  "absence of one. Add an operator that reaches it.")
            continue

        step = max(1, total // budget)
        survived, killed = [], 0
        for i in range(0, total, step):
            m = Mut(i); tree = m.visit(ast.parse(original))
            if m.desc is None: continue
            try:
                path.write_text(ast.unparse(ast.fix_missing_locations(tree)))
                # Not caught by the `except Exception` below on purpose: a
                # stale cache invalidates the whole run, not one sample.
                # (`subprocess_cache_path`'s aborts ride the same exit.)
                assert_fresh(path, subprocess_cache_path(path))
                t0 = time.time()
                if run(tests):
                    survived.append(m.desc)
                    print(f"    SURVIVED  {m.desc}  ({time.time()-t0:.0f}s)")
                else:
                    killed += 1
            except Exception as e:
                print(f"    skipped   {m.desc}: {type(e).__name__}")
            finally:
                path.write_text(original)
        n = killed + len(survived)
        print(f"    => killed {killed}/{n}, SURVIVED {len(survived)}/{n}")

if __name__ == "__main__":
    main()

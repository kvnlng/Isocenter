"""Sampled mutation probe: does the suite notice when behaviour changes?

Coverage says a line ran. It does not say a test would have failed had
that line done something else. This asks the second question: mutate one
decision in a module, run only the tests that import it, and see whether
anything goes red. A mutant that survives is a change to production
behaviour the suite cannot see.

Run it against the de-identification core, where a test that cannot fail
is not an inconvenience but a leak nobody is watching for:

    python -m scripts.mutation_probe                 # default targets, 30 samples each
    python -m scripts.mutation_probe 60              # a wider sample
    python -m scripts.mutation_probe 30 isocenter/session.py tests/test_session.py

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
"""

import ast, subprocess, sys, time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYTEST = [str(REPO / ".venv/bin/python"), "-m", "pytest", "-x", "-q", "--no-header", "-p", "no:randomly"]

# The tests to run for each target. Complete-ness matters more than it
# looks: a mutant that a test in this repo would kill, but which is not
# in this list, is reported as SURVIVED -- a phantom gap in the
# de-identification core that costs a human a real investigation.
# `tests/test_mutation_probe_targets.py` fails if a test file imports a
# target module and is not listed here. Extra entries are allowed and
# deliberate: `test_remediation_actions.py` exercises `remediation.py`
# without importing it, which no import scan can see.
TARGETS = {
    "isocenter/crypto.py": ["tests/test_crypto.py", "tests/test_reversibility.py"],
    "isocenter/privacy.py": ["tests/test_analysis.py", "tests/test_analysis_persistence.py",
                             "tests/test_audit_suppression.py", "tests/test_automation.py",
                             "tests/test_config_tags_shapes.py", "tests/test_multiprocessing.py",
                             "tests/test_mutation_gaps.py", "tests/test_ocr_formal.py",
                             "tests/test_persistence.py", "tests/test_privacy.py",
                             "tests/test_profile_end_to_end.py", "tests/test_remediation.py",
                             "tests/test_remediation_actions.py",
                             "tests/test_scaffold_features.py",
                             "tests/test_sr_anonymization.py"],
    "isocenter/remediation.py": ["tests/test_audit_suppression.py", "tests/test_deid_tags.py",
                                 "tests/test_mutation_gaps.py", "tests/test_persistence.py",
                                 "tests/test_remediation.py",
                                 "tests/test_remediation_accounting.py",
                                 "tests/test_remediation_actions.py",
                                 "tests/test_remediation_dates.py",
                                 "tests/test_scaffold_features.py"],
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

def run(tests):
    r = subprocess.run(PYTEST + tests, cwd=REPO, capture_output=True, text=True, timeout=900)
    return r.returncode == 0

def main():
    argv = sys.argv[1:]
    budget = int(argv[0]) if argv and argv[0].isdigit() else 30
    rest = argv[1:] if argv and argv[0].isdigit() else argv
    targets = {rest[0]: list(rest[1:])} if len(rest) >= 2 else TARGETS

    for mod, tests in targets.items():
        path = REPO / mod
        original = path.read_text()
        total = count_ops(original)

        # Control: unparsed-but-unmutated must still pass, or every
        # result below is an artefact of the harness rather than a finding.
        path.write_text(ast.unparse(ast.parse(original)))
        ok = run(tests)
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

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

TARGETS = {
    "isocenter/crypto.py": ["tests/test_crypto.py", "tests/test_reversibility.py"],
    "isocenter/privacy.py": ["tests/test_privacy.py", "tests/test_remediation.py",
                             "tests/test_mutation_gaps.py",
                             "tests/test_profile_end_to_end.py", "tests/test_audit_suppression.py",
                             "tests/test_sr_anonymization.py"],
    "isocenter/remediation.py": ["tests/test_remediation.py", "tests/test_remediation_actions.py",
                                 "tests/test_mutation_gaps.py",
                                 "tests/test_remediation_dates.py", "tests/test_deid_tags.py",
                                 "tests/test_remediation_accounting.py"],
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

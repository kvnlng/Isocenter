"""Every environment variable this package reads is documented (#331).

`isocenter/parallel.py` read `ISOCENTER_FORCE_PROCESSES` and nothing
said so: not `docs/environment.md`, not CLAUDE.md, not
`run_parallel()`'s own docstring. A lever you can only find by reading
the source is one a user cannot use and a maintainer can delete without
noticing anyone depended on it. Writing the row is a five-minute fix;
the reason this file exists is that the same five-minute fix was
already needed a second time and nobody knew -- the first run of this
sweep was red on **two** names, `ISOCENTER_FORCE_PROCESSES` and
`ISOCENTER_LOG_FILE` (`isocenter/logger.py`). A carve-out for the
second would have been the silent-skip pattern this tree has written
down twice (#162, `tests/test_doc_anchors.py`).

**The direction is read-but-undocumented, deliberately, and not the
reverse.** A name in the table that nothing reads is a different
defect, and this sweep cannot tell one apart from a variable read
through a spelling the pattern below does not recognise -- an
`os.environ` copied into a helper, say. Reporting a stale row as a
defect would make the guard red on prose that is merely generous, so it
grades only the direction it can be sure about.

**Anchored on the read call, never on the `ISOCENTER_` substring.**
`_ISOCENTER_REDACTION_HASH`, `_ISOCENTER_PIXEL_DTYPE` and
`_ISOCENTER_SOURCE_SOP_UID` are private DICOM attribute keys, not
environment variables, and a substring sweep pulls all three in and
demands rows for them. The pattern also requires a **string literal**
argument, which is what keeps `_env_int`'s and `_env_is`'s own
definitions -- `os.environ.get(name)` -- out of the result.

**Bound to `docs/environment.md` only, not to CLAUDE.md.** CLAUDE.md's
parallelism paragraph already omits `ISOCENTER_LOG_LEVEL`,
`ISOCENTER_DB_PATH`, `ISOCENTER_LOG_FILE` and
`ISOCENTER_WORKER_FAULTHANDLER`, and it points at `docs/environment.md`
rather than restating it. Requiring both places would be red on four
more names on the day it was written, and would turn a pointer into a
registry it was never written as.

No `scripts/mutation_probe.py` `TARGETS` entry: this is a pure text
check that imports no target module, so running it against every mutant
of a target would cost time for zero kill signal -- the argument
`tests/test_documented_api_exists.py` and `tests/test_source_citations.py`
both make for themselves.
"""
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parent.parent
PACKAGE = REPO / "isocenter"
REGISTRY = REPO / "docs" / "environment.md"

# A read call with a literal name. See the module docstring for why the
# anchor is the call and not the `ISOCENTER_` prefix.
_READ_SITE = re.compile(
    r"(?:_env_is|_env_int|os\.getenv|os\.environ\.get)\(\s*"
    r"[\"'](ISOCENTER_[A-Z0-9_]+)[\"']")


def _names_read():
    """`{name: [where it is read]}` across the package.

    Matched over the whole file rather than line by line: a read call
    whose literal wrapped to the next line would be invisible to a
    per-line sweep, and invisible is the one answer this guard must
    never give. The line number is derived from the match offset so the
    failure message can still say where to look.
    """
    found = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in _READ_SITE.finditer(text):
            lineno = text.count("\n", 0, match.start()) + 1
            found.setdefault(match.group(1), []).append(
                f"{path.relative_to(REPO).as_posix()}:{lineno}")
    return found


def test_every_environment_variable_the_package_reads_is_documented():
    """A lever nobody can find is a lever nobody has."""
    found = _names_read()

    # A broken pattern would find nothing and report a clean pass, which
    # is the failure mode this tree has recorded twice already.
    assert len(found) >= 8, (
        f"only {len(found)} environment reads found ({sorted(found)}); "
        "the sweep has stopped matching the code and this test would "
        "otherwise pass vacuously (#331)")

    registry = REGISTRY.read_text(encoding="utf-8")
    missing = sorted(name for name in found if name not in registry)

    assert not missing, (
        "these environment variables are read by the package and appear "
        f"nowhere in {REGISTRY.relative_to(REPO).as_posix()} (#331):\n    "
        + "\n    ".join(f"{name} -- read at {', '.join(found[name])}"
                        for name in missing))

"""What a test run added to the repository root (#707).

Pure functions over a directory listing, so they are testable against a
`tmp_path` and carry no pytest import. `conftest.py` calls them from
`pytest_sessionstart` and `pytest_sessionfinish`.
"""
from pathlib import Path

#: Entries tooling is entitled to create in the root during a run.
#: Prefix match. Extend this only for tooling -- never for a test's own
#: output, which belongs in `tmp_path`. Each `repo_root` test that
#: legitimately leaves something here gets its entry with the test's
#: name beside it.
ALLOWED_PREFIXES = (
    ".pytest_cache",
    "__pycache__",
    ".coverage",          # `.coverage` and `.coverage.<host>.<pid>.<rand>`
    ".test-map.json",     # scripts/test_map.py (#707)
)


def snapshot(root: Path) -> frozenset:
    return frozenset(entry.name for entry in Path(root).iterdir())


def new_entries(root: Path, before: frozenset) -> list:
    return sorted(
        name for name in snapshot(root) - before
        if not name.startswith(ALLOWED_PREFIXES))

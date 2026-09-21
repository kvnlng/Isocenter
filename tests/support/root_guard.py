"""What a test run added to the repository root (#707).

Pure functions over a directory listing, so they are testable against a
`tmp_path` and carry no pytest import. `conftest.py` calls them from
`pytest_sessionstart` and `pytest_sessionfinish`.
"""
from pathlib import Path

#: Entries tooling is entitled to create in the root during a run.
#: Prefix match. Extend this only for tooling -- never for a test's own
#: output, which belongs in `tmp_path`. A test that legitimately leaves
#: something in the root gets an entry in `ALLOWED_NAMES` below, with the
#: test's name beside it.
ALLOWED_PREFIXES = (
    ".pytest_cache",
    "__pycache__",
    ".coverage",          # `.coverage` and `.coverage.<host>.<pid>.<rand>`
    ".test-map.json",     # scripts/test_map.py (#707)
)

#: Entries allowed by exact name, for the one test that builds in the
#: root. test_packaging_contract.py's `built` fixture runs `setup.py
#: sdist bdist_wheel` with cwd=REPO; its outputs go to tmp_path, and these
#: are setuptools' working directories. Measured, #707: pointing
#: `egg_info --egg-base` elsewhere drops `isocenter.egg-info/` from the
#: sdist (397 entries, not 403), so the build stays in the root and the
#: directories are named here instead. Exact, not prefix: a test's own
#: `build.db` is still a stray.
ALLOWED_NAMES = frozenset({
    "build",               # test_packaging_contract.py::built (bdist)
    "isocenter.egg-info",  # test_packaging_contract.py::built (egg_info)
})


def snapshot(root: Path) -> frozenset:
    return frozenset(entry.name for entry in Path(root).iterdir())


def new_entries(root: Path, before: frozenset) -> list:
    return sorted(
        name for name in snapshot(root) - before
        if not name.startswith(ALLOWED_PREFIXES)
        and name not in ALLOWED_NAMES)

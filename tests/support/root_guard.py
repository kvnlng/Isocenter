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


def _allowed(name: str) -> bool:
    return name.startswith(ALLOWED_PREFIXES) or name in ALLOWED_NAMES


def snapshot(root: Path) -> dict:
    """`{name: (mtime_ns, size)}` for each regular file, `None` otherwise.

    Files carry a stamp so a write into a name that was already there is
    seen too (#720 review): a checkout that predates #707 holds
    `isocenter.log` and a dozen `test_*.db`/`*_pixels.bin`/`*.lock` names
    in its root, which is exactly where a stray relative write would land.
    Directories carry none, because a directory's mtime moves whenever
    anything is created inside it (`tests/__pycache__`, `.git/index.lock`),
    which is not a write into the root. Symlinks are not followed.
    """
    entries = {}
    for entry in Path(root).iterdir():
        try:
            st = entry.lstat()
        except FileNotFoundError:  # removed between listing and stat
            continue
        is_file = entry.is_file() and not entry.is_symlink()
        entries[entry.name] = (st.st_mtime_ns, st.st_size) if is_file else None
    return entries


def new_entries(root: Path, before: dict) -> list:
    return sorted(name for name in snapshot(root).keys() - before.keys()
                  if not _allowed(name))


def modified_entries(root: Path, before: dict) -> list:
    """Files that were in the root at `before` and have been rewritten."""
    now = snapshot(root)
    return sorted(
        name for name, stamp in before.items()
        if stamp is not None and name in now and now[name] != stamp
        and not _allowed(name))


def report(root: Path, before: dict):
    """The guard's one line, or None when the root is as it was.

    Blind, by design, to anything below the root's top level: a write
    anchored on `__file__` into `tests/` is not seen here.
    """
    named = ([f"{name} (new)" for name in new_entries(root, before)]
             + [f"{name} (modified)"
                for name in modified_entries(root, before)])
    if not named:
        return None
    return ("this run wrote into the repository root: " + ", ".join(named)
            + " -- a test wrote outside its tmp_path (#707)")

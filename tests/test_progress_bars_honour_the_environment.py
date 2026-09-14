"""`ISOCENTER_SHOW_PROGRESS=0` silences every progress bar (#540).

Until 0.9.8 the variable was read in one place, `_resolve_strategy`, so it
reached the bars `run_parallel` draws and no other. `anonymize()` drew
"Anonymizing Metadata", `release_memory()` -- which every `export()` runs
-- drew "Releasing Memory", and `lock_identities([...])` drew "Locking
Identities", all with the variable at `0`. `export(show_progress=False)`
drew "Releasing Memory" too, because the sweep took no argument.

Every negative case has a positive control beside it, the same site with
the variable unset: a capture that sees nothing at all would otherwise
pass every "absent" assertion for the wrong reason.
"""
import ast
import os
import pathlib

import pydicom
import pytest
from pydicom.data import get_testdata_file

from isocenter import Session

PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "isocenter"
PID = "PAT-540"


def _ingested_session(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    ds = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    ds.PatientID = PID
    ds.PatientName = "Test^Progress"
    ds.save_as(str(src / "a.dcm"))
    session = Session(str(tmp_path / "s.db"))
    session.ingest(str(src))
    return session


def _anonymize(session, tmp_path):
    session.anonymize(session.audit())


def _release_memory(session, tmp_path):
    session.release_memory()


def _lock_identities(session, tmp_path):
    session.lock_identities([PID])


def _export(session, tmp_path):
    session.export(str(tmp_path / "out"), use_compression=False)


def _prepare_lock(session, tmp_path):
    session.enable_reversible_anonymization(str(tmp_path / "k.key"))


#: site id -> (the bar's desc, the call that draws it, setup before capture)
SITES = {
    "anonymize": ("Anonymizing Metadata", _anonymize, None),
    "release_memory": ("Releasing Memory", _release_memory, None),
    "lock_identities": ("Locking Identities", _lock_identities, _prepare_lock),
    "export": ("Releasing Memory", _export, None),
}


def _drawn(tmp_path, capfd, site):
    desc, call, setup = SITES[site]
    session = _ingested_session(tmp_path)
    try:
        if setup:
            setup(session, tmp_path)
        capfd.readouterr()  # drop ingest's own output
        call(session, tmp_path)
        captured = capfd.readouterr()
    finally:
        session.close()
    return desc, captured.out + captured.err


@pytest.mark.parametrize("site", sorted(SITES))
def test_the_variable_at_zero_silences_the_bar(site, tmp_path, capfd,
                                               monkeypatch):
    monkeypatch.setenv("ISOCENTER_SHOW_PROGRESS", "0")
    desc, output = _drawn(tmp_path, capfd, site)
    assert desc not in output, (
        f"{site} drew {desc!r} with ISOCENTER_SHOW_PROGRESS=0:\n{output}")


@pytest.mark.parametrize("site", sorted(SITES))
def test_the_bar_is_drawn_when_the_variable_is_unset(site, tmp_path, capfd,
                                                     monkeypatch):
    monkeypatch.delenv("ISOCENTER_SHOW_PROGRESS", raising=False)
    desc, output = _drawn(tmp_path, capfd, site)
    assert desc in output, (
        f"positive control: {site} drew no {desc!r} bar with the variable "
        f"unset, so the capture cannot see bars at all:\n{output}")


def test_export_show_progress_false_silences_the_memory_release_bar(
        tmp_path, capfd, monkeypatch):
    monkeypatch.delenv("ISOCENTER_SHOW_PROGRESS", raising=False)
    session = _ingested_session(tmp_path)
    try:
        capfd.readouterr()
        session.export(str(tmp_path / "out"), use_compression=False,
                       show_progress=False)
        captured = capfd.readouterr()
    finally:
        session.close()
    assert "Releasing Memory" not in captured.out + captured.err


@pytest.mark.parametrize("value", ["0", "false", "off", "no", "FALSE", "Off"])
def test_progress_enabled_is_false_for_every_falsey_spelling(value,
                                                             monkeypatch):
    from isocenter.parallel import progress_enabled

    monkeypatch.setenv("ISOCENTER_SHOW_PROGRESS", value)
    assert progress_enabled(True) is False


@pytest.mark.parametrize("value", ["1", "", "yes", "true"])
def test_progress_enabled_follows_the_caller_otherwise(value, monkeypatch):
    from isocenter.parallel import progress_enabled

    monkeypatch.setenv("ISOCENTER_SHOW_PROGRESS", value)
    assert progress_enabled(True) is True
    assert progress_enabled(False) is False


def test_progress_enabled_follows_the_caller_when_unset(monkeypatch):
    from isocenter.parallel import progress_enabled

    monkeypatch.delenv("ISOCENTER_SHOW_PROGRESS", raising=False)
    assert progress_enabled() is True
    assert progress_enabled(False) is False


@pytest.mark.parametrize("value", ["0", "false", "off", "no", "FALSE"])
def test_run_parallel_resolves_its_bar_through_the_same_rule(value,
                                                             monkeypatch):
    """`_resolve_strategy` asks `progress_enabled` rather than keeping its
    own copy of the falsey set. Two copies of one variable's spelling are
    two answers to "is the bar off", and a copy that knew only `0` would
    pass `test_parallel_config.py`, which sets exactly `0`."""
    from isocenter.parallel import _resolve_strategy

    monkeypatch.setenv("ISOCENTER_SHOW_PROGRESS", value)
    strategy = _resolve_strategy(2, 1, None, False, False, True, "t", None)
    assert strategy.show_progress is False


def test_run_parallel_keeps_its_bar_when_the_variable_is_unset(monkeypatch):
    from isocenter.parallel import _resolve_strategy

    monkeypatch.delenv("ISOCENTER_SHOW_PROGRESS", raising=False)
    assert _resolve_strategy(2, 1, None, False, False, True, "t",
                             None).show_progress is True
    assert _resolve_strategy(2, 1, None, False, False, False, "t",
                             None).show_progress is False


def _is_tqdm(module):
    """`tqdm` or any submodule of it; `from . import x` has no module."""
    return module is not None and (module == "tqdm"
                                   or module.startswith("tqdm."))


def _tqdm_sites(sources=None):
    """Every `tqdm` import in the package, as `path:line`.

    `sources` is `{name: source text}`, for the detector's own tests; by
    default every module under the package.
    """
    if sources is None:
        sources = {str(path.relative_to(PACKAGE)): path.read_text(encoding="utf-8")
                   for path in sorted(PACKAGE.rglob("*.py"))}
    sites = []
    for name, text in sources.items():
        tree = ast.parse(text)
        for node in ast.walk(tree):
            named = None
            # `tqdm.auto`, `tqdm.std`, `tqdm.notebook` draw the same bar
            # past the same rule, and `from tqdm.auto import tqdm` is the
            # usual spelling (review of #589).
            if isinstance(node, ast.ImportFrom) and _is_tqdm(node.module):
                named = f"from {node.module} import"
            # The class re-exported by another module is the same
            # undisabled bar: `from .parallel import tqdm` binds exactly
            # what `parallel.progress_bar` wraps, and it is the name the
            # tests patch, so it is the one a contributor sees (review of
            # #589, round 2).
            elif isinstance(node, ast.ImportFrom) and any(
                    alias.name == "tqdm" for alias in node.names):
                named = f"from {node.module or '.'} import tqdm"
            elif isinstance(node, ast.Import) and any(
                    _is_tqdm(alias.name) for alias in node.names):
                named = "import tqdm"
            if named:
                sites.append(f"{name}:{node.lineno}")
    return sites


@pytest.mark.parametrize("spelling", [
    "from tqdm import tqdm",
    "from tqdm.auto import tqdm",
    "from tqdm.std import tqdm as bar",
    "import tqdm",
    "import tqdm.auto",
    "import tqdm.notebook as tn",
    "from .parallel import tqdm",
    "from .parallel import (progress_bar, tqdm)",
    "from isocenter.parallel import tqdm as t",
])
def test_the_detector_sees_every_spelling_of_a_tqdm_import(spelling):
    """The guard below is only as good as what it recognises. It knew
    `from tqdm import` and `import tqdm` alone, so `from tqdm.auto import
    tqdm` in `session.py` passed it (review of #589)."""
    # `zzz_session.py`, not `session.py`: the expected `path:line` below
    # is a string literal in this file, and `tests/test_source_citations.py`
    # sweeps this file and would grade it as a citation of the real
    # module (#535). The detector keys on the dict, so the name is free.
    assert _tqdm_sites({"zzz_session.py": f"import os\n{spelling}\n"}) == [
        "zzz_session.py:2"]


def test_the_detector_does_not_see_a_module_that_only_starts_with_tqdm():
    assert _tqdm_sites({"zzz_session.py": "import tqdmx\nfrom tqdm_extra import y\n"
                                          "from . import tqdm_like\n"}) == []


def test_only_parallel_imports_tqdm():
    """The bars one rule governs are the bars that go through one door.

    A module that imports `tqdm` itself can draw a bar the variable never
    reaches, which is how three of them did (#540), and how the private
    redaction loop in `services.py` did while no `Session` path reached it.
    Every bar goes through `parallel.progress_bar`, or through
    `run_parallel`'s own, and both ask `progress_enabled`.
    """
    outside = [site for site in _tqdm_sites()
               if not site.startswith(f"parallel.py:")]
    assert _tqdm_sites(), "found no tqdm import at all -- the walk is broken"
    assert not outside, (
        f"tqdm imported outside parallel.py, where ISOCENTER_SHOW_PROGRESS "
        f"is not consulted: {outside}; draw the bar with "
        f"parallel.progress_bar instead")

"""What a configuration does is pinned per `CONFIG_VERSION` (#782).

A minor bump is owed whenever the same file is applied differently to the
same input: findings, values a rule writes, tags a rule reaches, pixel
zones or date jitter (owner ruling on #762). `SCHEMA_BY_VERSION` checks
keys only. Measured on `main` at 7579d4df: `VR_DUMMY['PN']` changed from
`'ANONYMIZED'` to `'CHANGED'` left the policy fingerprint equal and every
test green, though every Patient's Name a profile replaces would have
been written differently under the same version.

Owner ruling Q8 A on #782: a behaviour digest
(`support/behaviour_digest.py`), pinned per version, and frozen for a
shipped version at the release cut, as `FROZEN_AT_1_0` freezes the profile
tables (`test_profile_editions.py`).

- `BEHAVIOUR_BY_VERSION` is the working table. A change that moves the
  digest under an unchanged `CONFIG_VERSION` is red here. The remedy is a
  minor bump with a new row (the old row is kept), or, before that version
  ships, the row updated with a CHANGELOG line saying why it is not a
  behaviour change.
- `SHIPPED_BEHAVIOUR` is a literal copy, checked once the package is 1.x.
  A PR can edit both dicts, so that check alone enforces nothing.
- What enforces it (review of #895): every row of `BEHAVIOUR_BY_VERSION`
  in the previous **final** release's copy of this file (`git show
  <tag>:<this file>`, the newest `vX.Y.Z` tag at or before
  `isocenter.__version__`, in `git tag --sort=-version:refname` order as
  `docs_decide.sh` reads it) must be in both tables here with the same
  hex. Final tags only (owner ruling Q9: 2.0 ships at 1.0.0, and a store
  an rc scanned re-audits), so an rc freezes nothing. Once a final tag
  carries this file, a PR that edits both rows is red, and the only
  green path for a behaviour change is a new `CONFIG_VERSION` row. Until
  then a row may move with both dicts and a CHANGELOG line. With no git
  checkout, no tags, no final tag (every tree before 1.0.0 is tagged), or
  a final that does not carry this file, the comparison skips with the
  cause named; it never passes without comparing. `RELEASING.md`
  ("Cutting a release") has the cut's check.

The digest runs real sessions, about four seconds under
`ISOCENTER_FORCE_THREADS=1`. It must give the same hex on 3.12 and 3.14t.
"""
import ast
import pathlib
import re
import subprocess

import pytest

import isocenter
from isocenter import config_manager
from support.behaviour_digest import behaviour_digest
from test_config_schema_version import SCHEMA_BY_VERSION

#: The digest of what each configuration version does, computed by
#: `support.behaviour_digest.behaviour_digest`. Recomputed on the B3 PR
#: (#782) after #879, #883, #784, #731 and #741 landed in it, and again
#: when the fixture was widened to see B1's #746 and #765 (review of #895).
BEHAVIOUR_BY_VERSION = {
    "2.0": "6eeeb91d8923e1132394c47bc5129d86e8e08bbba3513023f6d4cdde3738ee90",
}

#: A literal copy of the rows a release has shipped; never
#: `dict(BEHAVIOUR_BY_VERSION)`, which would pin nothing.
SHIPPED_BEHAVIOUR = {
    "2.0": "6eeeb91d8923e1132394c47bc5129d86e8e08bbba3513023f6d4cdde3738ee90",
}

_WHAT_TO_DO = (
    "the same file is applied differently under an unchanged CONFIG_VERSION; "
    "bump the minor and add a row (keep the old one), or, before the version "
    "ships, update the row with a CHANGELOG line saying why it is not a "
    "behaviour change (#782)")


@pytest.fixture(scope="module")
def digest(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("ISOCENTER_FORCE_THREADS", "1")
        return behaviour_digest(str(tmp_path_factory.mktemp("behaviour")))


def test_behaviour_matches_this_versions_row(digest):
    version = config_manager.CONFIG_VERSION
    assert version in BEHAVIOUR_BY_VERSION, (
        f"CONFIG_VERSION {version} has no BEHAVIOUR_BY_VERSION row; add one "
        f"with this digest, {digest} (#782)")
    assert digest == BEHAVIOUR_BY_VERSION[version], (
        f"behaviour digest {digest}, pinned {BEHAVIOUR_BY_VERSION[version]}: "
        f"{_WHAT_TO_DO}")


def test_a_shipped_versions_behaviour_never_moves(digest):
    """The working pin, live from the first 1.x version string: the digest
    equals the `SHIPPED_BEHAVIOUR` row, and each shipped row is in the
    working table unchanged. On its own this pins nothing a PR cannot
    move, since a PR can edit both dicts; the tag comparison below is what
    makes it hold after a release."""
    major = int(isocenter.__version__.split(".")[0])
    if major < 1:
        pytest.skip(f"isocenter {isocenter.__version__} is before 1.0")
    assert SHIPPED_BEHAVIOUR, "SHIPPED_BEHAVIOUR is empty in a 1.x (#782)"
    version = config_manager.CONFIG_VERSION
    if version in SHIPPED_BEHAVIOUR:
        assert digest == SHIPPED_BEHAVIOUR[version], (
            f"version {version} shipped with behaviour "
            f"{SHIPPED_BEHAVIOUR[version]}, and it is now {digest}: {_WHAT_TO_DO}")
    for shipped, frozen in SHIPPED_BEHAVIOUR.items():
        assert BEHAVIOUR_BY_VERSION.get(shipped) == frozen, (
            f"BEHAVIOUR_BY_VERSION[{shipped!r}] moved from the shipped "
            f"{frozen}; a shipped version's row never changes (#782)")


# --- The previous final release's rows (review of #895; owner ruling Q9) --

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_THIS_FILE = "tests/test_config_behaviour_is_versioned.py"
#: A final release tag: `vX.Y.Z` with no `a`, `b` or `rc` suffix.
_FINAL_TAG = re.compile(r"v([0-9]+)\.([0-9]+)\.([0-9]+)")
_VERSION = re.compile(r"([0-9]+)\.([0-9]+)\.([0-9]+)(.*)")
_TABLES = ("BEHAVIOUR_BY_VERSION", "SHIPPED_BEHAVIOUR")


def _git(repo, *args):
    """`git -C <repo> args`, as (returncode, stdout, stderr)."""
    done = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=False)
    return done.returncode, done.stdout, done.stderr.strip()


def _previous_final_tag(version, tags):
    """The newest final tag at or before `version`, from `tags` listed
    newest first (`git tag --list 'v*' --sort=-version:refname`, the
    listing `.github/scripts/docs_decide.sh` reads). Pre-release tags are
    skipped: owner ruling Q9 ships `CONFIG_VERSION` 2.0 at 1.0.0, and a
    store an rc scanned re-audits, so an rc freezes nothing. A pre-release
    `version` (`1.0.0rc9`) comes before its own final, and a later line's
    final is never previous to this tree. Not by reachability: release
    tags sit on `release/X.Y`, which `main` never reaches. None when no
    final is."""
    ours = _VERSION.fullmatch(version)
    assert ours, f"isocenter.__version__ {version!r} is not X.Y.Z[suffix]"
    release = tuple(int(part) for part in ours.groups()[:3])
    for tag in tags:
        final = _FINAL_TAG.fullmatch(tag)
        if not final:
            continue
        cut = tuple(int(part) for part in final.groups())
        if cut < release or (cut == release and not ours.group(4)):
            return tag
    return None


def _rows_in(source):
    """`BEHAVIOUR_BY_VERSION` and `SHIPPED_BEHAVIOUR` as `source` (this
    file's text at some commit) assigns them, read without importing it."""
    rows = {}
    for node in ast.parse(source).body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in _TABLES):
            rows[node.targets[0].id] = ast.literal_eval(node.value)
    return rows


def _moved_rows(released, ours):
    """Each row the release held that `ours` (both tables) does not hold
    with the same hex. A row added since is not a move."""
    moved = []
    for version, frozen in sorted(released.items()):
        for table, held in ours.items():
            if held.get(version) != frozen:
                moved.append(f"{table}[{version!r}] is {held.get(version)!r}, "
                             f"released as {frozen!r}")
    return moved


def _against_previous_final(repo, version, ours):
    """Compare `ours` with the rows the previous final release of `repo`
    shipped. Returns `(tag, moved)`, or `(None, why)` when there is
    nothing to compare with, `why` naming the cause."""
    code, _, err = _git(repo, "rev-parse", "--is-inside-work-tree")
    if code != 0:
        return None, (f"not a git checkout, so no release tag to compare "
                      f"with (an sdist?): {err}")
    code, listed, err = _git(repo, "tag", "--list", "v*",
                             "--sort=-version:refname")
    assert code == 0, f"git tag --list failed: {err}"
    tags = listed.split()
    if not tags:
        _, shallow, _ = _git(repo, "rev-parse", "--is-shallow-repository")
        return None, ("no v* tags in this checkout"
                      + (" (a shallow clone)" if shallow.strip() == "true" else "")
                      + "; `git fetch --tags origin` to compare")
    tag = _previous_final_tag(version, tags)
    if tag is None:
        return None, (f"no final release tag (vX.Y.Z) at or before isocenter "
                      f"{version}; pre-release tags freeze nothing (owner "
                      f"ruling Q9), so rows are held from the first final, "
                      f"v1.0.0")
    code, released_text, err = _git(repo, "show", f"{tag}:{_THIS_FILE}")
    if code != 0:
        return None, (f"{tag} does not carry {_THIS_FILE}, so it shipped no "
                      f"rows to hold: {err}")
    released = _rows_in(released_text)
    assert set(released) == set(_TABLES), (
        f"{tag}'s {_THIS_FILE} does not assign both tables")
    return tag, _moved_rows(released["BEHAVIOUR_BY_VERSION"], ours)


def test_the_previous_final_skips_pre_releases_and_later_lines():
    """The tag finder alone, on listed tags: no git needed."""
    tags = ["v1.1.0", "v1.0.1", "v1.0.0", "v1.0.0rc9", "v1.0.0rc8", "v0.9.8",
            "not-a-tag"]
    assert _previous_final_tag("1.0.0rc10", tags) == "v0.9.8"
    assert _previous_final_tag("1.0.0", tags) == "v1.0.0"
    assert _previous_final_tag("1.0.2", tags) == "v1.0.1"
    assert _previous_final_tag("1.1.0rc1", tags) == "v1.0.1"
    assert _previous_final_tag("0.9.0", tags) is None
    assert _previous_final_tag("1.0.0rc10", ["v1.0.0rc9", "v1.0.0rc8"]) is None
    assert _previous_final_tag("1.0.1", ["v1.0.1rc1", "v1.0.0"]) == "v1.0.0"


def test_a_moved_row_is_named_and_an_added_row_is_not():
    released = {"2.0": "aa"}
    assert _moved_rows(released, {"BEHAVIOUR_BY_VERSION": {"2.0": "aa", "2.1": "bb"},
                                  "SHIPPED_BEHAVIOUR": {"2.0": "aa"}}) == []
    assert _moved_rows(released, {"BEHAVIOUR_BY_VERSION": {"2.0": "cc"},
                                  "SHIPPED_BEHAVIOUR": {"2.0": "cc"}}) == [
        "BEHAVIOUR_BY_VERSION['2.0'] is 'cc', released as 'aa'",
        "SHIPPED_BEHAVIOUR['2.0'] is 'cc', released as 'aa'"]
    assert _moved_rows(released, {"BEHAVIOUR_BY_VERSION": {},
                                  "SHIPPED_BEHAVIOUR": {}}) == [
        "BEHAVIOUR_BY_VERSION['2.0'] is None, released as 'aa'",
        "SHIPPED_BEHAVIOUR['2.0'] is None, released as 'aa'"]


def _scratch_repo(root, released_hex, *tags):
    """A new git repository under `root` (never the real one) holding a
    copy of this file whose two tables say `released_hex` for 2.0, with
    each of `tags` on that commit."""
    repo = root / "scratch"
    copy = repo / _THIS_FILE
    copy.parent.mkdir(parents=True)
    text = (_ROOT / _THIS_FILE).read_text(encoding="utf-8")
    pinned = BEHAVIOUR_BY_VERSION["2.0"]
    assert text.count(f'"2.0": "{pinned}"') == 2
    copy.write_text(text.replace(f'"2.0": "{pinned}"', f'"2.0": "{released_hex}"'),
                    encoding="utf-8")
    ident = ["-c", "user.name=scratch", "-c", "user.email=scratch@example.invalid",
             "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false"]
    for args in (["init", "-q"], ["add", _THIS_FILE],
                 [*ident, "commit", "-q", "-m", "scratch"],
                 *[[*ident, "tag", tag] for tag in tags]):
        code, _, err = _git(repo, *args)
        assert code == 0, f"git {args}: {err}"
    return repo


def test_a_final_tag_whose_row_moved_is_red(tmp_path):
    """Both rows edited after a final release: the comparison names both."""
    repo = _scratch_repo(tmp_path, "aa" * 32, "v1.0.0")
    edited = {table: {"2.0": "bb" * 32} for table in _TABLES}
    tag, moved = _against_previous_final(repo, "1.0.1", edited)
    assert tag == "v1.0.0"
    assert moved == [f"{table}['2.0'] is {'bb' * 32!r}, released as {'aa' * 32!r}"
                     for table in _TABLES]
    tag, moved = _against_previous_final(repo, "1.0.1",
                                         {table: {"2.0": "aa" * 32} for table in _TABLES})
    assert (tag, moved) == ("v1.0.0", [])


def test_a_pre_release_tag_freezes_nothing(tmp_path):
    """Owner ruling Q9: an rc carrying a different row is not compared
    with; with no final, the comparison says so rather than passing."""
    repo = _scratch_repo(tmp_path, "aa" * 32, "v1.0.0rc9", "v1.0.0rc8", "v1.0.1rc1")
    edited = {table: {"2.0": "bb" * 32} for table in _TABLES}
    tag, why = _against_previous_final(repo, "1.0.0rc10", edited)
    assert tag is None
    assert "no final release tag (vX.Y.Z) at or before isocenter 1.0.0rc10" in why
    # An rc before this version, which a reader counting pre-releases
    # would take as the previous release.
    tag, why = _against_previous_final(repo, "1.0.1", edited)
    assert tag is None
    assert "no final release tag (vX.Y.Z) at or before isocenter 1.0.1" in why


def test_no_row_the_previous_final_release_shipped_has_moved():
    """The enforcement (review of #895): every row of `BEHAVIOUR_BY_VERSION`
    in the previous final release's copy of this file must be in both
    tables here with the same hex, so a PR that edits both dicts after a
    final release is red, and the only green path for a behaviour change
    is a new `CONFIG_VERSION` row. Final tags only (owner ruling Q9): an
    rc freezes nothing. The rows are read from this file's text at the
    tag (`git show <tag>:<this file>`), so no table here vouches for
    itself.

    Skips, each with its cause named, never a pass without comparing: no
    git checkout (an sdist), no `v*` tags (a shallow clone or one never
    fetched), no final tag at or before this version (every tree before
    1.0.0 is tagged), or a final that does not carry this file."""
    tag, result = _against_previous_final(
        _ROOT, isocenter.__version__,
        {"BEHAVIOUR_BY_VERSION": BEHAVIOUR_BY_VERSION,
         "SHIPPED_BEHAVIOUR": SHIPPED_BEHAVIOUR})
    if tag is None:
        pytest.skip(result)
    assert not result, (
        f"a row {tag} released has moved: {'; '.join(result)}. A shipped "
        f"version's behaviour never changes: bump CONFIG_VERSION's minor and "
        f"add a row (#782)")


def test_one_behaviour_row_per_schema_row():
    """Both tables say which versions exist; they must agree."""
    assert set(BEHAVIOUR_BY_VERSION) == set(SCHEMA_BY_VERSION)
    assert set(SHIPPED_BEHAVIOUR) <= set(BEHAVIOUR_BY_VERSION)


def test_the_scaffold_header_reads_the_constant(tmp_path, monkeypatch):
    """The scaffold's first line named `(v2.0)` as a literal, which a bump
    left stale. Kills the literal restored."""
    from isocenter.session import DicomSession
    monkeypatch.setattr(config_manager, "CONFIG_VERSION", "2.7")
    scaffold = tmp_path / "scaffold.yaml"
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.create_config(str(scaffold))
    first = scaffold.read_text(encoding="utf-8").splitlines()[0]
    assert first == "# Isocenter Privacy Configuration (v2.7)", first

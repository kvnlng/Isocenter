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
  in the previous release's copy of this file (`git show <tag>:<this
  file>`, the newest `v*` tag not after `isocenter.__version__`, in the
  release step's own order, pre-releases included) must be in both
  tables here with the same hex. Once a release tag carries this file, a
  PR that edits both rows is red, and the only green path for a
  behaviour change is a new `CONFIG_VERSION` row. Before then (every tag
  up to `v1.0.0rc8`), a row may move with both dicts and a CHANGELOG
  line. With no git checkout, no tags, or no tag carrying this file, the
  comparison skips with the cause named; it never passes without
  comparing. `RELEASING.md` ("Cutting a release") has the cut's check.

The digest runs real sessions, about four seconds under
`ISOCENTER_FORCE_THREADS=1`. It must give the same hex on 3.12 and 3.14t.
"""
import ast
import pathlib
import subprocess
import sys

import pytest

import isocenter
from isocenter import config_manager
from support.behaviour_digest import behaviour_digest
from test_config_schema_version import SCHEMA_BY_VERSION

#: The digest of what each configuration version does, computed by
#: `support.behaviour_digest.behaviour_digest`. Recomputed on the B3 PR
#: (#782) after #879, #883, #784, #731 and #741 landed in it.
BEHAVIOUR_BY_VERSION = {
    "2.0": "a3ea5ac8a0ad9d3a13b0fd52a4f0422d96156dd483f72ee582a559377e953a2f",
}

#: A literal copy of the rows a release has shipped; never
#: `dict(BEHAVIOUR_BY_VERSION)`, which would pin nothing.
SHIPPED_BEHAVIOUR = {
    "2.0": "a3ea5ac8a0ad9d3a13b0fd52a4f0422d96156dd483f72ee582a559377e953a2f",
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


# --- The previous release's rows (review of #895) -------------------------

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_THIS_FILE = "tests/test_config_behaviour_is_versioned.py"


def _git(*args):
    """`git -C <checkout> args`, as (returncode, stdout, stderr)."""
    done = subprocess.run(["git", "-C", str(_ROOT), *args],
                          capture_output=True, text=True, check=False)
    return done.returncode, done.stdout, done.stderr.strip()


def _release_order():
    """`scripts/output_fingerprint._tag_order`, the release step's own
    reading of a `v*` tag (`previous-tag`, RELEASING step 1): version
    order, pre-releases included, `a` < `b` < `rc` < final. One reader, so
    this test and the release step cannot disagree on which release came
    before. Imported, never skipped on: `scripts` is no declared extra, so
    a skip on it would break #107's rule (`test_skip_contract.py`); a tree
    without it is broken, as for `test_output_fingerprint.py`."""
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    from scripts import output_fingerprint  # noqa: PLC0415
    return output_fingerprint._tag_order


def _previous_release_tag(version, tags, order):
    """The newest `v*` tag at or before `version` by `order`, pre-releases
    included, as `previous-tag` reads them, but never one after this
    tree's version: a patch line's tree is not compared with a later
    line's release. Not by reachability: release tags sit on
    `release/X.Y`, and `main` reaches none of them. None when no tag
    is."""
    ours = order(f"v{version}")
    assert ours is not None, f"isocenter.__version__ {version!r} is not a release version"
    eligible = [tag for tag in tags if order(tag) is not None and order(tag) <= ours]
    return max(eligible, key=order) if eligible else None


def _rows_in(source):
    """`BEHAVIOUR_BY_VERSION` and `SHIPPED_BEHAVIOUR` as `source` (this
    file's text at some commit) assigns them, read without importing it."""
    rows = {}
    for node in ast.parse(source).body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in ("BEHAVIOUR_BY_VERSION",
                                           "SHIPPED_BEHAVIOUR")):
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


def test_the_previous_release_is_the_newest_tag_not_after_this_version():
    """The tag finder alone, on listed tags: no git needed."""
    order = _release_order()
    tags = ["v0.9.8", "v1.1.0", "v1.0.0rc8", "v1.0.1", "v1.0.0", "not-a-tag"]
    assert _previous_release_tag("1.0.0rc9", tags, order) == "v1.0.0rc8"
    assert _previous_release_tag("1.0.0rc8", tags, order) == "v1.0.0rc8"
    assert _previous_release_tag("1.0.0", tags, order) == "v1.0.0"
    assert _previous_release_tag("1.0.2", tags, order) == "v1.0.1"
    assert _previous_release_tag("1.1.0rc1", tags, order) == "v1.0.1"
    assert _previous_release_tag("0.9.0", tags, order) is None


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


def test_no_row_the_previous_release_shipped_has_moved():
    """The enforcement (review of #895): every row of `BEHAVIOUR_BY_VERSION`
    in the previous release's copy of this file must be in both tables
    here with the same hex, so a PR that edits both dicts after a release
    is red, and the only green path for a behaviour change is a new
    `CONFIG_VERSION` row. The previous release is the newest `v*` tag not
    after `isocenter.__version__`, in the release step's own order
    (pre-releases included). Its rows are read from this file's text at
    the tag (`git show <tag>:<this file>`), so no table here vouches for
    itself.

    Skips, each with its cause named, never a pass without comparing: no
    git checkout (an sdist), no `v*` tags (a shallow clone or one never
    fetched), no tag at or before this version, or a tag that does not
    carry this file (every release before it)."""
    code, _, err = _git("rev-parse", "--is-inside-work-tree")
    if code != 0:
        pytest.skip(f"not a git checkout, so no release tag to compare "
                    f"with (an sdist?): {err}")
    order = _release_order()
    code, listed, err = _git("tag", "--list", "v*")
    assert code == 0, f"git tag --list failed: {err}"
    tags = listed.split()
    if not tags:
        _, shallow, _ = _git("rev-parse", "--is-shallow-repository")
        pytest.skip("no v* tags in this checkout"
                    + (" (a shallow clone)" if shallow.strip() == "true" else "")
                    + "; `git fetch --tags origin` to compare")
    tag = _previous_release_tag(isocenter.__version__, tags, order)
    if tag is None:
        pytest.skip(f"no v* tag at or before isocenter {isocenter.__version__}")
    code, released_text, err = _git("show", f"{tag}:{_THIS_FILE}")
    if code != 0:
        pytest.skip(f"{tag} does not carry {_THIS_FILE}, so it shipped no "
                    f"rows to hold: {err}")
    released = _rows_in(released_text)
    assert set(released) == {"BEHAVIOUR_BY_VERSION", "SHIPPED_BEHAVIOUR"}, (
        f"{tag}'s {_THIS_FILE} does not assign both tables")
    moved = _moved_rows(released["BEHAVIOUR_BY_VERSION"],
                        {"BEHAVIOUR_BY_VERSION": BEHAVIOUR_BY_VERSION,
                         "SHIPPED_BEHAVIOUR": SHIPPED_BEHAVIOUR})
    assert not moved, (
        f"a row {tag} released has moved: {'; '.join(moved)}. A shipped "
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

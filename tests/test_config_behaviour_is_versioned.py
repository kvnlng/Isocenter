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
  Until the v1.0.0 tag, a change that moves the digest updates both dicts
  in the same PR, and its review checks that it did. After the tag
  `SHIPPED_BEHAVIOUR["2.0"]` never changes, so the only green path for a
  behaviour change is a new version row: the bump is enforced, not
  reviewed. `RELEASING.md` ("Cutting a release") has the cut's check.

The digest runs real sessions, about four seconds under
`ISOCENTER_FORCE_THREADS=1`. It must give the same hex on 3.12 and 3.14t.
"""
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
    """`FROZEN_AT_1_0`'s gate: live from the first 1.x version string."""
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

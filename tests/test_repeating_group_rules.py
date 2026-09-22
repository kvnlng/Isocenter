"""The table's repeating-group spellings are rule keys (#556).

Measured on 63a64158: a CT carrying two curve groups (5000, 5002) and two
overlay groups (6000, 6002) exported 30 of its 34 curve and overlay
elements under `basic`, the floor alike. Every curve element survived,
because Table E.1-1's `Curve Data (50xx,xxxx)` row had no rule and no
rule key could spell it; Curve Description and Curve Label held a name.
Removing Overlay Data (60xx,3000) left the rest of the Overlay Plane
module, whose Type 1 element it is (PS3.3 C.9-2), and Overlay
Description and Overlay Label, free text the table does not list,
reached the export holding a name.

Now `phi_tags` accepts `50xx,xxxx` / `60xx,xxxx` (a whole group) and
`50xx,eeee` / `60xx,eeee` (one element in every group), over the even
groups 5000-501E and 6000-601E only (PS3.5 7.6), with REMOVE or KEEP;
the most specific key wins. `basic@2026c` carries `50xx,xxxx` and
`60xx,xxxx` as REMOVE, the table's overlay rows folded into the latter.
"""
import sqlite3

import pydicom
import pydicom.data
import pytest
import yaml

from isocenter.privacy import PhiInspector
from isocenter.session import DicomSession


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")


def _build(path, overlay_bytes=2048):
    """§1.1's synthetic file: two curve groups, two overlay groups, and a
    private element beside each of 5001 and 6001."""
    ds = pydicom.dcmread(pydicom.data.get_testdata_file("CT_small.dcm"))
    for group in (0x5000, 0x5002):
        ds.add_new((group, 0x0005), "US", 1)                   # Curve Dimensions
        ds.add_new((group, 0x0010), "US", 2)                   # Number of Points
        ds.add_new((group, 0x0020), "CS", "TAC")               # Type of Data
        ds.add_new((group, 0x0022), "LO", "Curve drawn by Dr Jane Doe")
        ds.add_new((group, 0x0103), "US", 0)                   # Data Value Representation
        ds.add_new((group, 0x2500), "LO", "JDOE CURVE")        # Curve Label
        ds.add_new((group, 0x3000), "OW", bytes(range(8)))     # Curve Data
    for group in (0x6000, 0x6002):
        ds.add_new((group, 0x0010), "US", 64)                  # Overlay Rows
        ds.add_new((group, 0x0011), "US", 256)                 # Overlay Columns
        ds.add_new((group, 0x0022), "LO", "Overlay by Jane Doe")
        ds.add_new((group, 0x0040), "CS", "G")
        ds.add_new((group, 0x0050), "SS", [1, 1])
        ds.add_new((group, 0x0100), "US", 1)
        ds.add_new((group, 0x0102), "US", 0)
        ds.add_new((group, 0x1500), "LO", "JANE DOE MRN 12345")  # Overlay Label
        ds.add_new((group, 0x3000), "OW", b"\x01" * overlay_bytes)
        ds.add_new((group, 0x4000), "LT", "Overlay comment Jane")
    ds.add_new(0x50010010, "LO", "VENDOR")
    ds.add_new(0x60011000, "LO", "private beside the overlay")
    ds.save_as(path)
    return ds


def _is_curve_or_overlay(tag):
    return ((0x5000 <= tag.group <= 0x501E or 0x6000 <= tag.group <= 0x601E)
            and tag.group % 2 == 0)


def _run(tmp_path, phi_tags=None, profile="basic", remove_private=True,
         overlay_bytes=2048):
    src = tmp_path / "in"
    src.mkdir()
    source = _build(str(src / "ct.dcm"), overlay_bytes)
    config = tmp_path / "cfg.yaml"
    config.write_text(yaml.safe_dump({
        "privacy_profile": profile, "remove_private_tags": remove_private,
        "phi_tags": phi_tags or {}}), encoding="utf-8")
    db = str(tmp_path / "s.db")
    with DicomSession(db) as session:
        session.ingest(str(src))
        session.load_config(str(config))
        session.anonymize(session.audit())
        summary = session.export(str(tmp_path / "out"), use_compression=False)
    assert summary.written == 1, summary.failures
    (written,) = list((tmp_path / "out").rglob("*.dcm"))
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT action_type, details FROM audit_log").fetchall()
    return source, pydicom.dcmread(str(written)), rows


def _key(tag):
    return f"{tag.group:04x},{tag.element:04x}"


def test_basic_removes_every_curve_and_overlay_group(tmp_path):
    """Kills the mask not looked up; only groups 5000 and 6000 swept (5002
    and 6002 survive); the element mask looked up but not the group mask;
    and a removal left unrecorded."""
    source, out, rows = _run(tmp_path)

    left = [_key(e.tag) for e in out.iterall() if _is_curve_or_overlay(e.tag)]
    assert left == [], left
    expected = sorted(_key(e.tag) for e in source if _is_curve_or_overlay(e.tag))
    assert len(expected) == 34
    removed = [details for action, details in rows if action == "REMEDIATION_REMOVE"]
    for key in expected:
        assert len([d for d in removed if f"(Tag {key})" in d
                    or f"Tag {key}" in d]) == 1, (key, removed)


def test_a_private_group_beside_them_is_the_private_sweeps(tmp_path):
    """Kills a mask matching an odd group, which would remove private data
    the user chose to keep."""
    _, out, _ = _run(tmp_path, remove_private=False)
    assert out[0x50010010].value == "VENDOR"          # a private creator, LO
    # No creator for this block, so it is read back as UN bytes.
    assert b"private beside the overlay" in bytes(out[0x60011000].value)


def test_the_private_sweep_still_removes_them(tmp_path):
    """The other half: under `remove_private_tags: true` they go, by the
    sweep, which is the only thing that should touch an odd group."""
    _, out, rows = _run(tmp_path, remove_private=True)
    assert 0x50010010 not in out and 0x60011000 not in out


def test_keep_on_the_group_keeps_a_whole_overlay(tmp_path):
    """Kills Q3's fold undone: the table's `60xx,3000` row beside the
    group rule would remove the data under a KEEP of the group, and leave
    the module invalid."""
    source, out, _ = _run(tmp_path, {"60xx,xxxx": {"action": "KEEP"}})

    def overlay(ds):
        return sorted(_key(e.tag) for e in ds
                      if _is_curve_or_overlay(e.tag) and e.tag.group >= 0x6000)
    assert len(overlay(source)) == 20
    assert overlay(out) == overlay(source)
    assert out[0x60003000].value == b"\x01" * 2048
    assert out[0x60023000].value == b"\x01" * 2048
    # The curves are still the profile's to remove.
    assert not [e for e in out if 0x5000 <= e.tag.group <= 0x501E]


def test_a_concrete_key_beats_the_masks(tmp_path):
    """`6002,0022` is more specific than `60xx,xxxx`. Kills a concrete key
    ignored when a mask also matches, and a concrete key applied to every
    group."""
    _, out, _ = _run(tmp_path, {"6002,0022": {"action": "KEEP"}})
    assert out[0x60020022].value == "Overlay by Jane Doe"
    assert 0x60000022 not in out
    assert 0x60023000 not in out


def test_an_element_mask_beats_the_group_mask(tmp_path):
    """`60xx,0022` is more specific than `60xx,xxxx`. Kills the group mask
    read before the element mask."""
    _, out, _ = _run(tmp_path, {"60xx,0022": {"action": "KEEP"}})
    assert out[0x60000022].value == out[0x60020022].value == "Overlay by Jane Doe"
    assert [e for e in out if 0x6000 <= e.tag.group <= 0x601E
            and e.tag.element != 0x0022] == []


def test_the_tables_letter_is_one_rule_away(tmp_path):
    """A user who wants the table's letter keeps the group and removes the
    two rows: the data goes, and the rest of the module stays. Kills a
    concrete-element mask (`60xx,3000`) losing to the group mask."""
    _, out, _ = _run(tmp_path, {"60xx,xxxx": {"action": "KEEP"},
                                "60xx,3000": {"action": "REMOVE"},
                                "60xx,4000": {"action": "REMOVE"}})
    for group in (0x6000, 0x6002):
        assert (group, 0x3000) not in out and (group, 0x4000) not in out
        assert out[group, 0x0022].value == "Overlay by Jane Doe"
        assert out[group, 0x0010].value == 64


def test_a_large_overlay_is_accounted_once(tmp_path):
    """A 262144-byte Overlay Data never reaches the graph: ingest files a
    `DATA_LOSS` row for it. Kills the sweep inventing a removal row for
    data it never held, and the rest of the group left behind."""
    _, out, rows = _run(tmp_path, overlay_bytes=262144)
    assert not [e for e in out.iterall() if _is_curve_or_overlay(e.tag)]
    about = [(action, details) for action, details in rows if "6000,3000" in details]
    assert [action for action, _ in about] == ["DATA_LOSS"], about
    # The rest of the group was in the graph, so it has its removal row.
    assert [a for a, d in rows if "Tag 6000,0022" in d] == ["REMEDIATION_REMOVE"]


def test_none_leaves_them_all(tmp_path):
    """Under `privacy_profile: none` nothing names the groups, so they
    survive: the change is the profile's, not a hidden sweep."""
    source, out, _ = _run(tmp_path, profile="none", remove_private=False)
    assert (sorted(_key(e.tag) for e in out if _is_curve_or_overlay(e.tag))
            == sorted(_key(e.tag) for e in source if _is_curve_or_overlay(e.tag)))


# --- the loader ------------------------------------------------------------

DOORS = ("load_config", "audit_config_path", "set_phi_tag", "audit_assigned",
         "inspector", "external_profile")

#: Every action a mask may not take, and the string form (a REPLACE).
REFUSED_RULES = {
    "empty": ({"action": "EMPTY"}, "EMPTY"),
    "replace": ({"action": "REPLACE"}, "REPLACE"),
    "string-form": ("Overlay", "REPLACE"),
    "jitter": ({"action": "JITTER"}, "JITTER"),
}


def _yaml(path, data):
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return str(path)


def _through(door, tmp_path, key, rule):
    """Apply `{key: rule}` through `door`; return the session's db path,
    or None for the inspector."""
    if door == "inspector":
        PhiInspector(config_tags={key: rule})
        return None
    db = str(tmp_path / "s.db")
    with DicomSession(db) as session:
        if door == "load_config":
            session.load_config(_yaml(tmp_path / "cfg.yaml", {
                "privacy_profile": "none", "phi_tags": {key: rule}}))
        elif door == "audit_config_path":
            session.audit(config_path=_yaml(tmp_path / "cfg.yaml", {
                "privacy_profile": "none", "phi_tags": {key: rule}}))
        elif door == "external_profile":
            profile = _yaml(tmp_path / "p.yaml", {"phi_tags": {key: rule}})
            session.load_config(_yaml(tmp_path / "cfg.yaml",
                                      {"privacy_profile": profile}))
        elif door == "audit_assigned":
            session.configuration.phi_tags = {key: rule}
            session.audit()
        else:
            if not isinstance(rule, dict):
                pytest.skip("set_phi_tag cannot spell the string form")
            session.configuration.set_phi_tag(key, rule["action"])
    return db


@pytest.mark.parametrize("door", DOORS)
@pytest.mark.parametrize("case", sorted(REFUSED_RULES))
def test_a_mask_takes_only_remove_or_keep(tmp_path, door, case):
    """At every door. The in-code doors never reach `_validated_phi_tags`,
    so a check placed only there lets them through (L1 condition (a)).
    Kills the mask arm deleted, an action other than REMOVE and KEEP let
    through, and the check wired into only the loader. A refused call
    changes nothing: no rule is stored and no project secret minted."""
    rule, action = REFUSED_RULES[case]
    with pytest.raises(ValueError) as caught:
        _through(door, tmp_path, "60xx,xxxx", rule)
    message = str(caught.value)
    assert (f"phi_tags['60xx,xxxx'] is {action}; a repeating-group key (50xx "
            f"or 60xx) takes REMOVE or KEEP, because it names elements of "
            f"many VRs (#556)") in message, message
    db = tmp_path / "s.db"
    if db.exists():
        with sqlite3.connect(str(db)) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM project_secret").fetchone()[0] == 0


def test_set_phi_tag_leaves_the_policy_unchanged_when_a_mask_is_refused(tmp_path):
    """Kills the mask check placed after the store."""
    with DicomSession(str(tmp_path / "s.db")) as session:
        before = dict(session.configuration.phi_tags)
        with pytest.raises(ValueError, match="#556"):
            session.configuration.set_phi_tag("60xx,xxxx", "EMPTY")
        assert session.configuration.phi_tags == before


@pytest.mark.parametrize("key", ["50xx,xxxx", "60XX,XXXX", "60xx,0022", "50Xx,2500"])
def test_the_mask_spellings_that_load(tmp_path, key):
    """Either case of `x` (the loader lowercases every key, L1 condition
    (b)). Kills a regex that refuses an uppercase X."""
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.load_config(_yaml(tmp_path / "cfg.yaml", {
            "privacy_profile": "none", "phi_tags": {key: {"action": "REMOVE"}}}))
        assert session.configuration.phi_tags == {key.lower(): {"action": "REMOVE"}}


@pytest.mark.parametrize("key", ["70xx,xxxx", "60xx,xx22", "6xxx,0010",
                                 "60xx,xxxx,", "51xx,xxxx", "5001,xxxx"])
def test_the_mask_spellings_that_are_refused(tmp_path, key):
    """Kills a regex that admits another group, a partial mask, or a
    trailing character."""
    with DicomSession(str(tmp_path / "s.db")) as session:
        with pytest.raises(ValueError) as caught:
            session.load_config(_yaml(tmp_path / "cfg.yaml", {
                "privacy_profile": "none", "phi_tags": {key: {"action": "REMOVE"}}}))
    message = str(caught.value)
    assert f"phi_tags key {key!r} is not a 'gggg,eeee' tag" in message, message
    assert "or a repeating-group key such as '60xx,xxxx'" in message, message


def test_a_mask_does_not_reach_a_group_outside_the_range():
    """`6020` is past 601E, and `6001` is private: `60xx` names neither.
    Kills the range check missing from the lookup."""
    from isocenter.privacy import _rule_for  # pylint: disable=import-outside-toplevel
    tags = {"60xx,xxxx": {"action": "REMOVE"}, "50xx,0022": {"action": "KEEP"}}
    assert _rule_for(tags, "6000,0010") == {"action": "REMOVE"}
    assert _rule_for(tags, "601e,0010") == {"action": "REMOVE"}
    assert _rule_for(tags, "6020,0010") is None
    assert _rule_for(tags, "6001,0010") is None
    assert _rule_for(tags, "5fff,0010") is None
    assert _rule_for(tags, "501e,0022") == {"action": "KEEP"}
    assert _rule_for(tags, "5020,0022") is None
    assert _rule_for(tags, "0008,0022") is None

"""`create_config()`'s scaffold reloads to the session's own policy (#741).

The scaffold and `save()` diffed `phi_tags` two ways. `save()` names the
session's base and writes every rule that differs from that base's,
compared whole (#715); the scaffold always named `basic@2026c` and wrote
only the rules whose *action* differed. Measured on `main` at 7579d4df: a
basic session holding `0010,0010: {REPLACE, value: X}` scaffolded to a file
whose reload had no `value`, so `X` was lost; and a `privacy_profile: none`
session holding one rule scaffolded under `basic@2026c`, which reloaded to
648 rules.

Owner ruling Q6 B: the scaffold names the session's own base and diffs
whole rules against it, through the same helper as `save()`. The floor
keeps its teaching spelling, `privacy_profile: basic@2026c` plus the
research defaults as editable lines, which reloads to the same rules. Q7
A: a failed write raises its `OSError`, as `save()` does, where the
scaffold logged and returned.
"""
import pytest
import yaml

from isocenter import profiles
from isocenter.config_manager import ConfigLoader
from isocenter.session import DicomSession


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def _session_on(tmp_path, base):
    """A session whose configuration stands on `base`."""
    session = DicomSession(str(tmp_path / "s.db"))
    if base == "basic":
        session.load_config(_write(tmp_path, "c.yaml", "privacy_profile: basic\n"))
    elif base == "none":
        session.load_config(_write(
            tmp_path, "c.yaml",
            "privacy_profile: none\nphi_tags:\n  '0010,0010': {action: REMOVE, name: Name}\n"))
    elif base == "external":
        profile = _write(tmp_path, "p.yaml",
                         "phi_tags:\n  '0010,0010': {action: REMOVE, name: Name}\n"
                         "  '0008,0080': {action: EMPTY}\n")
        session.load_config(_write(tmp_path, "c.yaml", f"privacy_profile: {profile}\n"))
    else:
        assert base == "floor"
    return session


CHANGES = {
    # Each rewrites Patient's Name's rule, keeping all but one part.
    "value": lambda rule: {**rule, "action": "REPLACE", "value": "Project-X"},
    "name": lambda rule: {**rule, "name": "Renamed in code"},
    "action": lambda rule: {**rule, "action": "KEEP"},
}


@pytest.mark.parametrize("change", sorted(CHANGES))
@pytest.mark.parametrize("base", ["floor", "basic", "none", "external"])
def test_the_scaffold_reloads_to_the_sessions_policy(tmp_path, base, change):
    """Main: the value and name rows were lost (an action-only diff), and
    every `none` session came back under 648 rules of `basic@2026c`.
    Kills the action-only diff restored, and the base forced to
    `FLOOR_BASE`."""
    with _session_on(tmp_path, base) as session:
        tags = session.configuration.phi_tags
        tags["0010,0010"] = CHANGES[change](dict(tags["0010,0010"]))
        expected = {tag: (dict(rule) if isinstance(rule, dict) else rule)
                    for tag, rule in tags.items()}
        scaffold = tmp_path / "scaffold.yaml"
        session.create_config(str(scaffold))

    reloaded, _, _, _, _ = ConfigLoader.load_unified_config(str(scaffold))
    assert reloaded == expected


@pytest.mark.parametrize("base, written", [
    ("floor", "basic@2026c"), ("basic", "basic@2026c"), ("none", "none"),
    ("external", "p.yaml")])
def test_the_scaffold_names_the_sessions_base(tmp_path, base, written):
    with _session_on(tmp_path, base) as session:
        scaffold = tmp_path / "scaffold.yaml"
        session.create_config(str(scaffold))
    named = yaml.safe_load(scaffold.read_text(encoding="utf-8"))["privacy_profile"]
    if base == "external":
        assert named == str(tmp_path / written)
    else:
        assert named == written


def test_a_floor_scaffold_still_teaches_the_research_defaults(tmp_path):
    """Guard (today's behaviour): the floor is spelled `basic@2026c` plus
    exactly `RESEARCH_DEFAULTS`, so the user sees the defaults as lines
    to edit, and the file reloads to the floor."""
    scaffold = tmp_path / "scaffold.yaml"
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.create_config(str(scaffold))
    data = yaml.safe_load(scaffold.read_text(encoding="utf-8"))
    assert data["privacy_profile"] == "basic@2026c"
    assert data["phi_tags"] == profiles.RESEARCH_DEFAULTS
    reloaded, _, _, _, _ = ConfigLoader.load_unified_config(str(scaffold))
    assert reloaded == profiles.FLOOR_POLICY


def test_a_failed_write_raises(tmp_path):
    """Owner ruling Q7 A. Main: the `OSError` was logged and the call
    returned, so a script went on as though the file existed. Kills the
    `except OSError` swallow restored."""
    with DicomSession(str(tmp_path / "s.db")) as session:
        with pytest.raises(OSError):
            session.create_config(str(tmp_path / "no-such-directory" / "c.yaml"))


def test_a_policy_missing_a_base_rule_is_refused_as_save_refuses_it(tmp_path):
    """A rule deleted from `phi_tags` would come back on reload from the
    base the file names. `save()` refuses that (#715); the scaffold, on
    the same helper, refuses it with the same words and writes nothing.
    Main: the action-only diff omitted the rule and the reload brought it
    back."""
    with _session_on(tmp_path, "basic") as session:
        del session.configuration.phi_tags["0010,0010"]
        session.configuration.config_path = str(tmp_path / "saved.yaml")
        with pytest.raises(ValueError) as from_save:
            session.configuration.save()
        scaffold = tmp_path / "scaffold.yaml"
        with pytest.raises(ValueError) as from_scaffold:
            session.create_config(str(scaffold))
    assert str(from_scaffold.value) == str(from_save.value)
    assert "has no rule for 0010,0010" in str(from_scaffold.value)
    assert not scaffold.exists()

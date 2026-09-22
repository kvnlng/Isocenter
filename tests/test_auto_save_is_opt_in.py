"""When a configuration file is written (#715).

Until 1.0, `add_rule()`, `update_rule()`, `delete_rule()` and
`set_phi_tag()` rewrote the file `load_config()` read, after every call,
whether or not the user wanted the file touched; and `save()` returned
silently with no file, and printed a WARNING and returned when the write
failed. Now the four change memory only, unless the session turns on
`configuration.auto_save`, and `save()` raises (owner rulings Q2-Q4).

Every file here is under `tmp_path`, and the refusal tests follow L1's
sentinel pattern: memory and the file's bytes are what they were.

**Why this file imports what it does.** `IsocenterConfiguration` through
`isocenter.configuration`, so that module's probe row is charged.
"""
import copy
import os

import pytest
import yaml

from isocenter.configuration import IsocenterConfiguration
from isocenter.session import DicomSession as Session

SEVEN_LINE_CONFIG = """\
# Site policy for the registry export.
# Reviewed by the privacy office.
version: "2.0"
privacy_profile: basic   # the PS3.15 basic profile
date_jitter: {min_days: -30, max_days: -10}
remove_private_tags: true
machines: []
"""

#: `SEVEN_LINE_CONFIG` with one machine rule, so `update_rule` and
#: `delete_rule` have something to change.
WITH_A_RULE = SEVEN_LINE_CONFIG.replace(
    "machines: []", "machines:\n  - {serial_number: SN1, redaction_zones: []}")

#: Each mutator, and what it leaves in memory.
MUTATORS = {
    "add_rule": (lambda c: c.add_rule("SN2", zones=[[0, 4, 0, 4]]),
                 lambda c: c.get_rule("SN2") is not None),
    "update_rule": (lambda c: c.update_rule("SN1", {"model_name": "M9"}),
                    lambda c: c.get_rule("SN1")["model_name"] == "M9"),
    "delete_rule": (lambda c: c.delete_rule("SN1"),
                    lambda c: c.get_rule("SN1") is None),
    "set_phi_tag": (lambda c: c.set_phi_tag("0008,0080", "KEEP"),
                    lambda c: c.phi_tags["0008,0080"]["action"] == "KEEP"),
}
FOUR = pytest.mark.parametrize("name", sorted(MUTATORS))


def _write(tmp_path, text=WITH_A_RULE, name="c.yaml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _policy(configuration):
    return copy.deepcopy({"rules": configuration.rules,
                          "phi_tags": configuration.phi_tags})


@FOUR
def test_a_loaded_file_is_not_rewritten(tmp_path, name):
    """Kills `save()` still called unconditionally, one row per method."""
    path = _write(tmp_path)
    change, changed = MUTATORS[name]
    with Session(str(tmp_path / "s.db")) as session:
        session.load_config(str(path))
        change(session.configuration)
        assert changed(session.configuration)
    assert path.read_text(encoding="utf-8") == WITH_A_RULE


@FOUR
def test_auto_save_writes_after_each_change(tmp_path, name, capsys):
    """Kills the flag inverted, one method ignoring it, and the
    memory-only notice printed under auto-save."""
    path = _write(tmp_path)
    change, _ = MUTATORS[name]
    with Session(str(tmp_path / "s.db")) as session:
        session.load_config(str(path))
        session.configuration.auto_save = True
        capsys.readouterr()
        change(session.configuration)
        assert "in memory only" not in capsys.readouterr().out
        expected = _policy(session.configuration)
    assert path.read_text(encoding="utf-8") != WITH_A_RULE
    with Session(str(tmp_path / "t.db")) as session:
        session.load_config(str(path))
        assert _policy(session.configuration) == expected


def test_auto_save_is_the_sessions_choice(tmp_path):
    """It survives `load_config()`, and writes the newly loaded file.
    Kills `load_config` resetting the flag, and a write to the first
    path."""
    a = _write(tmp_path, name="a.yaml")
    b = _write(tmp_path, name="b.yaml")
    with Session(str(tmp_path / "s.db")) as session:
        session.load_config(str(a))
        session.configuration.auto_save = True
        session.load_config(str(b))
        assert session.configuration.auto_save is True
        session.configuration.add_rule("SN2")
    assert a.read_text(encoding="utf-8") == WITH_A_RULE
    assert yaml.safe_load(b.read_text(encoding="utf-8"))["machines"][-1]["serial_number"] == "SN2"


def test_auto_save_is_off_by_default(tmp_path):
    """Kills the default flipped."""
    assert IsocenterConfiguration().auto_save is False
    with Session(str(tmp_path / "s.db")) as session:
        assert session.configuration.auto_save is False


@pytest.mark.parametrize("call", [
    lambda c: c.add_rule("SN"),
    lambda c: c.set_phi_tag("0008,0080", "KEEP"),
    # Nothing would change, and it still refuses: the check is about the
    # session's setting, not about the call.
    lambda c: c.delete_rule("NOPE"),
], ids=["add_rule", "set_phi_tag", "delete_rule of an absent serial"])
def test_auto_save_with_no_file_refuses_before_the_change(tmp_path, call):
    """Kills a silent no-op, and the check placed after the change."""
    with Session(str(tmp_path / "s.db")) as session:
        listing = sorted(os.listdir(tmp_path))
        session.configuration.auto_save = True
        before = _policy(session.configuration)
        with pytest.raises(ValueError, match="has no file to write"):
            call(session.configuration)
        assert _policy(session.configuration) == before
        assert sorted(os.listdir(tmp_path)) == listing


def _unwritable(tmp_path):
    """A path under a regular file: `NotADirectoryError` whatever the
    permissions, and as root."""
    plain = tmp_path / "plain_file"
    plain.write_text("", encoding="utf-8")
    return str(plain / "c.yaml")


@FOUR
def test_a_failed_auto_save_leaves_memory_as_it_was(tmp_path, name):
    """Kills the swallowed WARNING (0.9.8), a change kept after its write
    failed, and a restore of `rules` only (the `set_phi_tag` row)."""
    path = _write(tmp_path)
    change, _ = MUTATORS[name]
    with Session(str(tmp_path / "s.db")) as session:
        session.load_config(str(path))
        session.configuration.config_path = _unwritable(tmp_path)
        session.configuration.auto_save = True
        before = _policy(session.configuration)
        with pytest.raises(OSError):
            change(session.configuration)
        assert _policy(session.configuration) == before


def test_a_failed_auto_save_keeps_the_rule_get_rule_handed_out(tmp_path):
    """`get_rule` documents a reference. After a failed auto-save the
    caller's dict is still the configuration's, and unchanged. Kills a
    restore that swaps in a deep copy (the caller's reference goes
    stale) and an update applied to the live rule before the write."""
    path = _write(tmp_path)
    with Session(str(tmp_path / "s.db")) as session:
        session.load_config(str(path))
        rule = session.configuration.get_rule("SN1")
        session.configuration.config_path = _unwritable(tmp_path)
        session.configuration.auto_save = True
        with pytest.raises(OSError):
            session.configuration.update_rule("SN1", {"model_name": "M9"})
        assert session.configuration.get_rule("SN1") is rule
        assert "model_name" not in rule


def test_a_refused_save_under_auto_save_restores_memory(tmp_path):
    """The base-rule refusal (`test_a_saved_config_names_its_profile.py`)
    raised from inside a mutator leaves the change undone. Kills that
    refusal escaping without the restore."""
    path = _write(tmp_path)
    with Session(str(tmp_path / "s.db")) as session:
        session.load_config(str(path))
        del session.configuration.phi_tags["0010,0010"]
        session.configuration.auto_save = True
        before = copy.deepcopy(session.configuration.phi_tags["0008,0080"])
        with pytest.raises(ValueError, match="0010,0010"):
            session.configuration.set_phi_tag("0008,0080", "KEEP")
        assert session.configuration.phi_tags["0008,0080"] == before
    assert path.read_text(encoding="utf-8") == WITH_A_RULE


def test_save_with_no_file_raises(tmp_path):
    """Kills the silent return (0.9.8)."""
    with Session(str(tmp_path / "s.db")) as session:
        listing = sorted(os.listdir(tmp_path))
        with pytest.raises(ValueError, match="has no file to write"):
            session.configuration.save()
        assert sorted(os.listdir(tmp_path)) == listing


def test_save_raises_the_write_error(tmp_path, capsys):
    """Kills the swallow: 0.9.8 printed a WARNING and returned."""
    configuration = IsocenterConfiguration(config_path=_unwritable(tmp_path))
    with pytest.raises(NotADirectoryError):
        configuration.save()
    assert "WARNING: Failed to auto-save" not in capsys.readouterr().out


NOTICE = "in memory only"


def _notices(capsys):
    return capsys.readouterr().out.count(NOTICE)


def test_the_first_memory_only_change_says_so(tmp_path, capsys):
    """Owner ruling Q4: a 0.9.x script that relied on auto-save is told,
    once, that its file stopped following its edits. Kills no notice, a
    notice on every call, the flag not reset by `save()` or by
    `load_config()`, and a notice with no file."""
    path = _write(tmp_path)
    with Session(str(tmp_path / "s.db")) as session:
        session.load_config(str(path))
        capsys.readouterr()
        session.configuration.add_rule("SN2")
        out = capsys.readouterr().out
        assert out.count(NOTICE) == 1, out
        assert str(path) in out and "save()" in out and "auto_save" in out, out
        session.configuration.add_rule("SN3")
        assert _notices(capsys) == 0

        session.configuration.save()
        session.configuration.add_rule("SN4")
        assert _notices(capsys) == 1

        session.load_config(str(path))
        capsys.readouterr()
        session.configuration.set_phi_tag("0008,0080", "KEEP")
        assert _notices(capsys) == 1

    with Session(str(tmp_path / "bare.db")) as session:
        session.configuration.add_rule("SN2")
        assert _notices(capsys) == 0


def test_a_failed_save_does_not_mark_the_file_current(tmp_path, capsys):
    """Only a write that succeeded puts the file back in step. Kills the
    in-step flag set before the write: the next change would announce the
    file as if it had just been saved."""
    path = _write(tmp_path)
    with Session(str(tmp_path / "s.db")) as session:
        session.load_config(str(path))
        session.configuration.add_rule("SN2")
        session.configuration.config_path = _unwritable(tmp_path)
        with pytest.raises(OSError):
            session.configuration.save()
        capsys.readouterr()
        session.configuration.add_rule("SN3")
        assert _notices(capsys) == 0


def test_a_bare_session_with_a_path_set_by_hand_is_told(tmp_path, capsys):
    """A session with a file to fall out of step with gets the notice,
    whoever set the path."""
    with Session(str(tmp_path / "s.db")) as session:
        session.configuration.config_path = str(tmp_path / "c.yaml")
        capsys.readouterr()
        session.configuration.add_rule("SN2")
        assert _notices(capsys) == 1


def test_auto_save_writes_nothing_a_refused_rule_would_have_changed(tmp_path):
    """L1's validation stays ahead of the write. Kills it moved after the
    save inside the new helper."""
    path = _write(tmp_path)
    with Session(str(tmp_path / "s.db")) as session:
        session.load_config(str(path))
        session.configuration.auto_save = True
        before = _policy(session.configuration)
        with pytest.raises(ValueError, match="0010,0020"):
            session.configuration.set_phi_tag("0010,0020", "REMOVE")
        with pytest.raises(ValueError, match="unknown key 'redaction_zone'"):
            session.configuration.update_rule("SN1", {"redaction_zone": []})
        assert _policy(session.configuration) == before
    assert path.read_text(encoding="utf-8") == WITH_A_RULE



@pytest.mark.parametrize("auto_save", [False, True], ids=["memory only", "auto-save"])
def test_delete_rule_still_says_whether_it_removed_one(tmp_path, auto_save):
    """`delete_rule`'s return value is unchanged by #715 (spec §2.6),
    through the new helper. Kills the removal's result dropped on the way
    out of `_apply`, and an absent serial reported as removed (probe
    survivors on this branch before this test)."""
    path = _write(tmp_path)
    with Session(str(tmp_path / "s.db")) as session:
        session.load_config(str(path))
        session.configuration.auto_save = auto_save
        assert session.configuration.delete_rule("NOPE") is False
        assert session.configuration.delete_rule("SN1") is True
        assert session.configuration.delete_rule("SN1") is False


def test_a_written_change_puts_the_file_back_in_step(tmp_path, capsys):
    """A change auto-save wrote leaves the file current, so a change after
    auto-save is turned off again is announced. Kills the in-step flag
    left as it was by an auto-saved change (a probe survivor on this
    branch before this test)."""
    path = _write(tmp_path)
    with Session(str(tmp_path / "s.db")) as session:
        session.load_config(str(path))
        session.configuration.add_rule("SN2")
        session.configuration.auto_save = True
        session.configuration.add_rule("SN3")
        session.configuration.auto_save = False
        capsys.readouterr()
        session.configuration.add_rule("SN4")
        assert _notices(capsys) == 1


def test_deleting_an_absent_rule_changes_nothing(tmp_path, capsys):
    """`delete_rule` writes `config_path` only when auto-save is on *and*
    a rule was removed (spec §2.1.3). Under auto-save a no-op leaves a
    hand-written file's bytes, comments included, as they were; with it
    off, a no-op prints no notice and leaves the file counted as in step,
    so the next real change is the one announced. Kills the early return
    removed, which sent the no-op through `_apply` (review of #742,
    finding 1)."""
    path = _write(tmp_path)
    with Session(str(tmp_path / "s.db")) as session:
        session.load_config(str(path))
        session.configuration.auto_save = True
        assert session.configuration.delete_rule("NOPE") is False
        assert path.read_text(encoding="utf-8") == WITH_A_RULE

        session.configuration.auto_save = False
        capsys.readouterr()
        assert session.configuration.delete_rule("NOPE") is False
        assert _notices(capsys) == 0
        assert session.configuration.delete_rule("SN1") is True
        assert _notices(capsys) == 1
    assert path.read_text(encoding="utf-8") == WITH_A_RULE

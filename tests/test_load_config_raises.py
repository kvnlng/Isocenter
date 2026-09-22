"""`load_config()` raises, and leaves the configuration as it was (#456).

Measured on `168fdd6` (`scratchpad/brief-096m/m456.py`, 21 inputs):
`Session.load_config` wrapped the loader in `except Exception`, printed
`Load failed: ...` beside an ERROR log carrying the same text, reset
`rules = []`, `phi_tags = {}`, `privacy_profile = None`, and returned
`None`. So a config that failed validation did not just fail to load: it
wiped the policy the session already had, and a script that ignored
stdout went on to audit against nothing. Four malformed shapes were not
even refused -- `action: OBLITERATE` (scanned as REPLACE), a list-shaped
`phi_tags`, an int rule, an unknown `privacy_profile` (warned and
dropped) -- and `date_jitter: soon` failed *after* the assignments, in
`load_config`'s own print, leaving `date_jitter='soon'` beside the
emptied fields and `config_path` pointing at the failed file.

Now every validation failure is `ValueError` and a missing file is
`FileNotFoundError`, both raised before anything is assigned. The
parametrised test sets a sentinel in each of the six fields first and
asserts all six survive, which is what kills validate-after-assign.
"""
import logging

import pytest

from isocenter.config_manager import ConfigLoader  # noqa: F401  (probe row)
from isocenter.session import DicomSession


#: name -> (file suffix, file text or None for "no such file",
#: expected exception, a fragment the message must carry or None).
CASES = {
    "missing_path": (".yaml", None, FileNotFoundError, None),
    "json_extension": (".json", "phi_tags: {}\n", ValueError, "YAML file"),
    "yaml_syntax": (".yaml", "phi_tags: {\n  bad: [\n", ValueError, "Invalid YAML"),
    "empty_file": (".yaml", "", ValueError, "mapping"),
    "scalar_root": (".yaml", "just a string\n", ValueError, "mapping"),
    "list_root": (".yaml", "- serial_number: X\n", ValueError, "mapping"),
    "rule_without_serial": (".yaml", "machines:\n  - model_name: X\n",
                            ValueError, "serial_number"),
    "zones_not_a_list": (".yaml",
                         "machines:\n  - serial_number: X\n    redaction_zones: 5\n",
                         ValueError, "redaction_zones"),
    "zone_of_three": (".yaml",
                      "machines:\n  - serial_number: X\n    redaction_zones: [[1,2,3]]\n",
                      ValueError, "ROI"),
    "zone_negative": (".yaml",
                      "machines:\n  - serial_number: X\n    redaction_zones: [[-1,2,3,4]]\n",
                      ValueError, "non-negative"),
    "zone_start_after_end": (".yaml",
                             "machines:\n  - serial_number: X\n    redaction_zones: [[5,2,3,4]]\n",
                             ValueError, "Start > End"),
    "zone_of_strings": (".yaml",
                        "machines:\n  - serial_number: X\n    redaction_zones: [[a,b,c,d]]\n",
                        ValueError, "integers"),
    "unknown_action": (".yaml",
                       "phi_tags:\n  '0010,0010': {action: OBLITERATE, name: PN}\n",
                       ValueError, "0010,0010"),
    "phi_tags_a_list": (".yaml", "phi_tags:\n  - '0010,0010'\n", ValueError, "phi_tags"),
    "tag_value_an_int": (".yaml", "phi_tags:\n  '0010,0010': 7\n", ValueError, "0010,0010"),
    "unknown_profile": (".yaml", "privacy_profile: comprehensive\nphi_tags: {}\n",
                        ValueError, "comprehensive"),
    "missing_custom_profile_file": (".yaml",
                                    "privacy_profile: /no/such/profile.yaml\n",
                                    ValueError, "/no/such/profile.yaml"),
    "date_jitter_a_word": (".yaml", "date_jitter: soon\n", ValueError, "date_jitter"),
    "date_jitter_wrong_keys": (".yaml", "date_jitter: {days: 3}\n", ValueError, "date_jitter"),
    # O5 (review of #509): the right keys with a value that is not an int.
    "date_jitter_values_not_ints": (".yaml", "date_jitter: {min_days: a, max_days: 5}\n",
                                    ValueError, "date_jitter"),
    # A key that is not 'gggg,eeee' hex matched no tag the scan reads, so
    # the rule was a silent no-op: '8,80' for 0008,0080, or a keyword.
    "tag_key_not_padded": (".yaml", "phi_tags:\n  '8,80': {action: REMOVE}\n",
                           ValueError, "'8,80'"),
    "tag_key_a_keyword": (".yaml", "phi_tags:\n  PatientName: {action: REMOVE}\n",
                          ValueError, "'PatientName'"),
    "machines_a_mapping": (".yaml", "machines: {a: 1}\n", ValueError, "machines"),
    "machines_of_strings": (".yaml", "machines:\n  - X\n", ValueError, "machines"),
}


def _set_sentinels(configuration, tmp_path):
    """A prior configuration in every field `load_config` writes.

    Assigned rather than set through `set_phi_tag`/`add_rule`: those
    print the memory-only notice for a set `config_path`, and with
    `auto_save` on would write it (#715). The path sits
    under `tmp_path` so that even a regression that saves writes nothing
    into the repository.
    """
    configuration.phi_tags = {"9999,0001": {"action": "REMOVE", "name": "sentinel"}}
    configuration.rules = [{"serial_number": "PRIOR"}]
    configuration.date_jitter = {"min_days": -7, "max_days": -7}
    configuration.remove_private_tags = False
    configuration.privacy_profile = "prior"
    configuration.config_path = str(tmp_path / "prior.yaml")
    return {
        "phi_tags": dict(configuration.phi_tags),
        "rules": list(configuration.rules),
        "date_jitter": dict(configuration.date_jitter),
        "remove_private_tags": False,
        "privacy_profile": "prior",
        "config_path": str(tmp_path / "prior.yaml"),
    }


@pytest.mark.parametrize("name", sorted(CASES))
def test_a_config_the_loader_rejects_raises_and_changes_nothing(name, tmp_path):
    """Each §1.2 input raises the named exception, and all six fields keep
    their sentinel. Kills: the `except Exception` that swallowed and reset
    (no raise, fields emptied); a missing file wrapped as `ValueError`;
    YAML syntax bypassing `_load_yaml` (`yaml.parser.ParserError`); a
    `TypeError`/`AttributeError` leaking from a non-mapping root; an
    unknown `action`, a list `phi_tags`, an int rule, an unknown profile
    or a malformed `date_jitter`/`machines` accepted; and any assignment
    moved above the validation (the `date_jitter: soon` half-state)."""
    suffix, text, expected, fragment = CASES[name]
    path = tmp_path / f"{name}{suffix}"
    if text is not None:
        path.write_text(text, encoding="utf-8")

    with DicomSession(str(tmp_path / "s.db")) as session:
        before = _set_sentinels(session.configuration, tmp_path)
        with pytest.raises(expected) as excinfo:
            session.load_config(str(path))
        after = {field: getattr(session.configuration, field) for field in before}

    if fragment is not None:
        assert fragment in str(excinfo.value), str(excinfo.value)
    assert after == before


def test_an_external_profile_with_no_phi_tags_mapping_raises(tmp_path):
    """An external profile file is read for its `phi_tags:` mapping. One
    without it had its root dict used as the tags, so `privacy_profile`
    and every other top-level key became a "tag" and the rules nested
    wrongly were dropped (review of #509, `extprof.py`). Refused, naming
    the profile file, and nothing is assigned. The file here holds only
    well-formed tag keys at its root, so the key check cannot be what
    refuses it. Kills the root-as-tags fallback restored."""
    profile = tmp_path / "prof.yaml"
    profile.write_text("'0008,0080': {action: KEEP}\n'0010,0010': {action: KEEP}\n",
                       encoding="utf-8")
    config = tmp_path / "cfg.yaml"
    config.write_text(f"privacy_profile: {profile}\n", encoding="utf-8")

    with DicomSession(str(tmp_path / "s.db")) as session:
        before = _set_sentinels(session.configuration, tmp_path)
        with pytest.raises(ValueError) as excinfo:
            session.load_config(str(config))
        after = {field: getattr(session.configuration, field) for field in before}
    assert str(profile) in str(excinfo.value)
    assert "phi_tags" in str(excinfo.value)
    assert after == before


def test_a_valid_config_still_loads(tmp_path):
    """The positive control: a well-formed file loads, returns `None`, and
    sets every field. Kills a loader broken wholesale (every case above
    would still pass against a `load_config` that raised on everything)."""
    config = tmp_path / "good.yaml"
    config.write_text(
        "privacy_profile: basic\n"
        "phi_tags:\n  '0018,1030': {action: REMOVE, name: Protocol}\n"
        "date_jitter: {min_days: -30, max_days: -10}\n"
        "remove_private_tags: false\n"
        "machines:\n  - serial_number: SN-1\n    redaction_zones: [[0, 10, 0, 20]]\n",
        encoding="utf-8")

    with DicomSession(str(tmp_path / "s.db")) as session:
        assert session.load_config(str(config)) is None
        c = session.configuration
        assert c.phi_tags["0018,1030"] == {"action": "REMOVE", "name": "Protocol"}
        assert c.privacy_profile == "basic@2026c"
        assert c.rules[0]["serial_number"] == "SN-1"
        assert c.date_jitter == {"min_days": -30, "max_days": -10}
        assert c.remove_private_tags is False
        assert c.config_path == str(config)


# `test_an_integer_date_jitter_is_still_a_fixed_shift` pinned `date_jitter: 5`
# as a shorthand #456 deliberately kept. The owner deleted it before the 1.0
# freeze (#713, ruling Q2: one spelling per behaviour), so the pin is
# inverted in `test_config_scalar_types.py::test_an_integer_date_jitter_is_refused`.


def test_audit_config_path_raises_the_same_way(tmp_path):
    """`audit(config_path=)` no longer falls back to a plain tag-file
    reader when the unified loader refuses a file. Measured before: a rule
    with no `serial_number` -- which the loader rejects -- was audited
    against happily, and a `.json` file, which `load_config` refuses, was
    accepted here. Kills the fallback kept."""
    broken = tmp_path / "broken_rule.yaml"
    broken.write_text("phi_tags:\n  '0018,1030': {action: REMOVE, name: P}\n"
                      "machines:\n  - model_name: X\n", encoding="utf-8")
    as_json = tmp_path / "tags.json"
    as_json.write_text('{"phi_tags": {"0018,1030": "Protocol"}}', encoding="utf-8")

    with DicomSession(str(tmp_path / "s.db")) as session:
        with pytest.raises(ValueError, match="serial_number"):
            session.audit(config_path=str(broken))
        with pytest.raises(ValueError, match="YAML file"):
            session.audit(config_path=str(as_json))
        with pytest.raises(FileNotFoundError):
            session.audit(config_path=str(tmp_path / "nope.yaml"))


def test_the_failure_is_not_printed_as_a_line_and_a_log(tmp_path, capsys, caplog):
    """No `Load failed` print, and no ERROR log: the exception is the
    report. The mutation probe wrote this up as "a print beside an ERROR
    log that carries the same text" -- a survivor whose silence was
    `load_config` swallowing the failure. Kills the print or the log
    reinstated beside the raise."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("machines:\n  - model_name: X\n", encoding="utf-8")

    with DicomSession(str(tmp_path / "s.db")) as session:
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="isocenter"):
            with pytest.raises(ValueError):
                session.load_config(str(bad))

    assert "Load failed" not in capsys.readouterr().out
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], [
        r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert "Load failed" not in caplog.text

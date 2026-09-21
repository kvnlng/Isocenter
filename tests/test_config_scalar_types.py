"""Every scalar the configuration loader reads is read with its type
(#713), and `date_jitter` has one spelling with ordered bounds.

Measured on 63a64158: scalars were stored as written.
`remove_private_tags: "false"` is a non-empty string and removed the
private tags; a bare `remove_private_tags:` loaded `None`, which is falsy
and kept them, the opposite of the default. `serial_number: 12345` loaded
as an int and `serial_number: 0123` as the int 83 (YAML 1.1 octal);
neither ever equals a Device Serial Number, so the rule matched nothing.
`date_jitter: 7` was an undocumented second spelling of `{min_days: 7,
max_days: 7}`, and `{min_days: 10, max_days: -10}` loaded and was swapped
silently by `RemediationService` (owner rulings Q2 and Q3: both refused).
"""
import pytest
import yaml

from isocenter.config_manager import ConfigLoader, validate_phi_policy
from isocenter.session import DicomSession

FIELDS = ("phi_tags", "rules", "date_jitter", "remove_private_tags",
          "privacy_profile", "config_path")


def _set_sentinels(configuration, tmp_path):
    """A prior configuration in every field `load_config` writes, assigned
    rather than set through a method that would auto-save."""
    configuration.phi_tags = {"9999,0001": {"action": "REMOVE", "name": "sentinel"}}
    configuration.rules = [{"serial_number": "PRIOR"}]
    configuration.date_jitter = {"min_days": -7, "max_days": -7}
    configuration.remove_private_tags = False
    configuration.privacy_profile = "prior"
    configuration.config_path = str(tmp_path / "prior.yaml")
    return {field: yaml.safe_load(yaml.safe_dump(getattr(configuration, field)))
            for field in FIELDS}


def _refused(tmp_path, text, name="cfg.yaml"):
    """The message `load_config` raises for `text`; asserts `ValueError`
    and that every field kept its sentinel."""
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    with DicomSession(str(tmp_path / "s.db")) as session:
        before = _set_sentinels(session.configuration, tmp_path)
        with pytest.raises(ValueError) as caught:
            session.load_config(str(path))
        after = {field: getattr(session.configuration, field) for field in FIELDS}
    assert after == before
    return str(caught.value)


def _loaded(tmp_path, text, name="cfg.yaml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return ConfigLoader.load_unified_config(str(path))


# --- remove_private_tags --------------------------------------------------


@pytest.mark.parametrize("value, shown", [
    ('"false"', "'false' (str)"), ('"true"', "'true' (str)"),
    ("0", "0 (int)"), ("1", "1 (int)"), ("", "None (NoneType)"),
    ('"no"', "'no' (str)")])
def test_remove_private_tags_must_be_a_bool(tmp_path, value, shown):
    """Kills: the check deleted; `isinstance(x, int)` (lets 0 and 1 in,
    bool being an int); a `bool(x)` coercion; null treated as absent."""
    message = _refused(tmp_path, f"remove_private_tags: {value}\n")
    assert "'remove_private_tags' must be true or false" in message, message
    assert f"got {shown}" in message, message


def test_a_quoted_false_says_why_it_would_have_read_as_true(tmp_path):
    message = _refused(tmp_path, 'remove_private_tags: "false"\n')
    assert "non-empty string, which reads as true" in message, message


@pytest.mark.parametrize("value, expected", [
    ("true", True), ("false", False), ("yes", True), ("no", False)])
def test_yaml_booleans_load_as_booleans(tmp_path, value, expected):
    """Kills an over-eager check refusing YAML 1.1 booleans."""
    _, _, _, remove_private, _ = _loaded(tmp_path, f"remove_private_tags: {value}\n")
    assert remove_private is expected


def test_an_absent_remove_private_tags_is_true(tmp_path):
    """Kills the default changed."""
    _, _, _, remove_private, _ = _loaded(tmp_path, "privacy_profile: basic\n")
    assert remove_private is True


# --- serial_number --------------------------------------------------------


@pytest.mark.parametrize("serial, shown", [("12345", "12345 (int)"), ("0123", "83 (int)")])
def test_a_numeric_serial_is_refused(tmp_path, serial, shown):
    """Kills the check deleted, and a `str()` coercion (which would store
    `"83"` for `0123`)."""
    message = _refused(tmp_path, f"machines:\n  - serial_number: {serial}\n")
    assert "Rule #0: 'serial_number' must be a string" in message, message
    assert f"got {shown}" in message, message
    assert "quote it" in message and "octal" in message, message


def test_a_quoted_numeric_serial_loads_as_written(tmp_path):
    _, rules, _, _, _ = _loaded(tmp_path, 'machines:\n  - serial_number: "0123"\n')
    assert rules[0]["serial_number"] == "0123"


@pytest.mark.parametrize("line", ["serial_number:", 'serial_number: ""'])
def test_an_empty_serial_is_still_missing(tmp_path, line):
    """The existing refusal, kept for null and the empty string."""
    message = _refused(tmp_path, f"machines:\n  - {line}\n")
    assert "Missing 'serial_number'" in message, message


# --- Every other string in the schema --------------------------------------

#: One row per string-typed key, each set to the int 5. Test data, not
#: parsed from anywhere: one row per check, so each deleted check has its
#: own red row.
STRING_KEYS = {
    "manufacturer": ("machines:\n  - serial_number: SN1\n    manufacturer: 5\n",
                     "Rule #0 (SN1): 'manufacturer' must be a string, got 5 (int)"),
    "model_name": ("machines:\n  - serial_number: SN1\n    model_name: 5\n",
                   "Rule #0 (SN1): 'model_name' must be a string, got 5 (int)"),
    "comment": ("machines:\n  - serial_number: SN1\n    comment: 5\n",
                "Rule #0 (SN1): 'comment' must be a string, got 5 (int)"),
    "note": ("machines:\n  - serial_number: SN1\n"
             "    redaction_zones: [{roi: [0, 4, 0, 4], note: 5}]\n",
             "Rule #0 (SN1), Zone #0: 'note' must be a string, got 5 (int)"),
    "name": ("phi_tags:\n  '0008,0080': {action: REMOVE, name: 5}\n",
             "phi_tags['0008,0080'] name must be a string, got int"),
}


@pytest.mark.parametrize("key", sorted(STRING_KEYS))
def test_every_schema_string_is_type_checked(tmp_path, key):
    """Kills any one string check dropped."""
    text, fragment = STRING_KEYS[key]
    message = _refused(tmp_path, text)
    assert fragment in message, message


def test_a_phi_rule_name_assigned_in_code_is_type_checked():
    """The `name` check is on every door a rule comes in by, not only the
    loader's `_validated_phi_tags`: `validate_phi_policy` is what
    `set_phi_tag`, `audit()` and `PhiInspector` call. Kills the check in
    the loader's arm only."""
    with pytest.raises(ValueError, match="name must be a string"):
        validate_phi_policy({"0008,0080": {"action": "REMOVE", "name": 5}},
                            "session.configuration.phi_tags")


#: Each optional metadata string set to null. 0.9.x auto-save wrote null
#: in the rule fields (review of #728, finding 1); nothing reads any of
#: the five, so null is read as absent in all of them.
NULL_STRINGS = {
    "manufacturer": "machines:\n  - serial_number: SN1\n    manufacturer:\n",
    "model_name": "machines:\n  - serial_number: SN1\n    model_name:\n",
    "comment": "machines:\n  - serial_number: SN1\n    comment:\n",
    "note": ("machines:\n  - serial_number: SN1\n"
             "    redaction_zones: [{roi: [0, 4, 0, 4], note: null}]\n"),
    "name": "phi_tags:\n  '0008,0080': {action: REMOVE, name: null}\n",
}


@pytest.mark.parametrize("key", sorted(NULL_STRINGS))
def test_a_null_optional_string_loads_as_absent(tmp_path, key):
    """One row per field, so a null refused in any one is its own red row.
    (Paired with `test_every_schema_string_is_type_checked`, which keeps a
    non-null non-string refused: the exemption is null's alone.)"""
    _loaded(tmp_path, NULL_STRINGS[key])


@pytest.mark.parametrize("line", ["manufacturer: 0", "model_name: false", "comment: []"])
def test_a_falsy_non_string_is_still_refused(tmp_path, line):
    """The exemption is null's, not every falsy value's. Kills
    `rule.get(key) and ...` in place of `is not None`."""
    message = _refused(tmp_path, f"machines:\n  - serial_number: SN1\n    {line}\n")
    assert "must be a string" in message, message


def test_a_null_phi_rule_name_is_absent_on_every_door():
    """`validate_phi_policy` -- `set_phi_tag`, `audit()`, `PhiInspector` --
    reads a null `name` as the loader does."""
    validate_phi_policy({"0008,0080": {"action": "REMOVE", "name": None}},
                        "session.configuration.phi_tags")


# --- date_jitter ----------------------------------------------------------


def test_an_integer_date_jitter_is_refused(tmp_path):
    """Owner ruling Q2: the single-int shorthand was an undocumented
    second spelling of `{min_days: N, max_days: N}` that no writer ever
    produced; one spelling per behaviour. The refusal gives the spelling
    that means what the int did. Kills the int arm kept."""
    message = _refused(tmp_path, "privacy_profile: basic\ndate_jitter: -5\n")
    assert "'date_jitter' must be {min_days: int, max_days: int}" in message, message
    assert "{min_days: -5, max_days: -5}" in message, message


def test_a_fixed_shift_is_spelled_with_equal_bounds(tmp_path):
    """What replaces the int form loads, and means a fixed shift."""
    _, _, jitter, _, _ = _loaded(
        tmp_path, "privacy_profile: basic\ndate_jitter: {min_days: -5, max_days: -5}\n")
    assert jitter == {"min_days": -5, "max_days": -5}


def test_date_jitter_bounds_the_wrong_way_round_are_refused(tmp_path):
    """Owner ruling Q3: a pair the wrong way round has at least one bound
    wrong and the loader cannot know which; it was swapped silently.
    Kills the check deleted, and `>=` (which would refuse equal bounds,
    covered above)."""
    message = _refused(tmp_path, "date_jitter: {min_days: 10, max_days: -10}\n")
    assert "min_days 10 is greater than max_days -10" in message, message


def test_a_refused_scalar_leaves_the_session_as_it_was(tmp_path):
    """A good policy, good machines and a good jitter, then one bad
    scalar: nothing is assigned. Kills a check moved into `load_config`
    after the assignments."""
    message = _refused(
        tmp_path,
        "privacy_profile: basic\n"
        "phi_tags:\n  '0018,1030': {action: REMOVE, name: Protocol}\n"
        "machines:\n  - serial_number: SN1\n    redaction_zones: [[0, 4, 0, 4]]\n"
        "date_jitter: {min_days: -30, max_days: -10}\n"
        'remove_private_tags: "no"\n')
    assert "remove_private_tags" in message, message

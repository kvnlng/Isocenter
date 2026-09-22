"""The safe-export suggestion must be a config Isocenter can read (#20).

When `export(check_burned_in=True)` finds PHI it prints the one
actionable instruction in the whole safety report: a config fragment that
would resolve the findings. That fragment used to be JSON with `//`
comments and a trailing comma -- not valid JSON, and user-facing configs
are YAML only, so even valid JSON would have been the wrong format.

These tests assert the fragment *parses and loads*, rather than matching
its text. Matching text is what let the previous format survive: the
strings were exactly as intended and the document was still unusable.
"""
import yaml

from isocenter.config_manager import ConfigLoader, load_unified_config
from isocenter.privacy import PhiFinding
from isocenter.session import _print_suggested_config, _report_phi_findings


COUNTS = {"0010,0010": 3, "0008,0020": 1, "0009,1001": 2}


def _emit(capsys, counts=None):
    _print_suggested_config(counts if counts is not None else COUNTS)
    out = capsys.readouterr().out
    # The fragment is everything from the first mapping key onward; the
    # lines above it are prose addressed to the operator.
    start = out.index("phi_tags:")
    return out[start:]


def test_the_suggested_fragment_parses_as_yaml(capsys):
    parsed = yaml.safe_load(_emit(capsys))

    assert isinstance(parsed, dict)
    assert set(parsed["phi_tags"]) == set(COUNTS)


def test_every_suggested_tag_is_given_a_removing_action(capsys):
    parsed = yaml.safe_load(_emit(capsys))

    for tag, rule in parsed["phi_tags"].items():
        assert rule["action"] == "REMOVE", f"{tag} -> {rule}"


def test_the_fragment_round_trips_through_the_config_loader(tmp_path, capsys):
    """Parsing as YAML is not enough; the loader has to accept the shape."""
    path = tmp_path / "suggested.yaml"
    path.write_text(_emit(capsys), encoding="utf-8")

    # `privacy_profile: none`, so the result is the fragment's rules alone.
    path.write_text("privacy_profile: none\n" + path.read_text(encoding="utf-8"),
                    encoding="utf-8")
    loaded, _, _, _, _ = ConfigLoader.load_unified_config(str(path))

    assert set(loaded) == set(COUNTS)
    assert loaded["0010,0010"]["action"] == "REMOVE"


def test_the_counts_survive_as_yaml_comments(capsys):
    """The counts are why the old format was invalid -- `// Found 3 times`
    is not JSON. YAML has comments, so they cost nothing here."""
    text = _emit(capsys)

    assert "# Found 3 times" in text
    assert "//" not in text
    # A trailing comma was the other half of the old invalidity.
    assert not any(l.rstrip().endswith(",") for l in text.splitlines())


def test_tag_names_come_from_the_shipped_mapping(capsys):
    """`_suggested_tag_name` recognised three tags and called everything
    else `unknown_tag`, while the default policy already named more. It
    reads `FLOOR_POLICY` since #495, whose names are the profile's PS3.6
    spellings."""
    parsed = yaml.safe_load(_emit(capsys))

    assert parsed["phi_tags"]["0010,0010"]["name"] == "Patient's Name"
    assert parsed["phi_tags"]["0008,0020"]["name"] == "Study Date"


def test_an_unrecognised_tag_still_produces_a_usable_rule(capsys):
    """A private vendor tag has no name to look up, and the fragment must
    stay loadable rather than omitting it."""
    parsed = yaml.safe_load(_emit(capsys))

    rule = parsed["phi_tags"]["0009,1001"]
    assert rule["action"] == "REMOVE"
    assert rule["name"]


# ---------------------------------------------------------------------------
# #587 -- every suggested rule is one the config can hold and the pipeline
# honours
# ---------------------------------------------------------------------------

def test_patient_id_is_suggested_as_its_pseudonym_not_removed(capsys):
    """Patient ID can only be kept or replaced by its keyed pseudonym.

    The fragment suggested `REMOVE` on 0010,0020 like every other tag. The
    ID is what keeps two patients apart -- `anonymize()` merges patients
    that share one (#548) -- so a removed or emptied ID would merge every
    patient it reached, and the tag-policy work (#537) refuses the rule at
    load. The rule that resolves a Patient ID finding is `REPLACE` with no
    `value:`, which is the pseudonym.
    """
    parsed = yaml.safe_load(_emit(capsys, {"0010,0020": 1, "0010,0010": 1}))

    rule = parsed["phi_tags"]["0010,0020"]
    assert rule["action"] == "REPLACE", rule
    assert "value" not in rule and "replacement" not in rule, rule
    assert rule["name"] == "Patient ID"
    # Every other tag still gets the removing rule.
    assert parsed["phi_tags"]["0010,0010"]["action"] == "REMOVE"


def _load_as_a_config(tmp_path, fragment):
    """The fragment pasted into a config, through the loader `load_config`
    uses, the one loader since #729 deleted `load_phi_config`.
    `privacy_profile: none` so the result is the fragment's rules
    alone, not the floor merged beneath them."""
    path = tmp_path / "suggested.yaml"
    path.write_text("privacy_profile: none\n" + fragment, encoding="utf-8")
    return load_unified_config(str(path))["phi_tags"]


def test_the_patient_id_rule_round_trips_through_the_config_loader(
        tmp_path, capsys):
    loaded = _load_as_a_config(tmp_path, _emit(capsys, {"0010,0020": 2}))

    assert loaded["0010,0020"]["action"] == "REPLACE"
    assert "value" not in loaded["0010,0020"]


def _finding(tag, field_name, value="SENTINEL^587"):
    return PhiFinding(entity_uid="1.2.3", entity_type="Instance",
                      field_name=field_name, value=value,
                      reason="New Leak (Uncovered)", tag=tag)


def test_a_finding_with_no_tag_is_never_a_phi_tags_key(capsys):
    """A rule is keyed on `gggg,eeee` or it is not a rule.

    The table labels a finding by `finding.tag or finding.field_name`,
    and the fragment reused those labels as keys. A finding with no tag --
    burned-in text from `verification.py`, or one reloaded from the
    store's `phi_findings` table, which keeps no tag -- became a rule
    keyed on its field name, which `load_config` refuses: the one
    actionable instruction in the report did not load. It stays in the table, and
    the fragment says it has no rule rather than inventing one.
    """
    _report_phi_findings([
        _finding(None, "PixelData[Frame=0]"),
        _finding(None, "patient_name"),
        _finding("0010,0010", "patient_name"),
    ])
    out = capsys.readouterr().out
    table, fragment = out[:out.index("phi_tags:")], out[out.index("phi_tags:"):]

    parsed = yaml.safe_load(fragment)
    assert list(parsed["phi_tags"]) == ["0010,0010"], parsed
    assert "PixelData[Frame=0]" in table, (
        "the tagless finding must still be listed in the table")
    assert "2 finding(s)" in out, out
    assert "SENTINEL^587" not in out


def test_the_fragment_with_a_tagless_finding_loads(tmp_path, capsys):
    _report_phi_findings([_finding(None, "patient_id"),
                          _finding("0008,0090", "Referring Physician's Name")])
    out = capsys.readouterr().out

    loaded = _load_as_a_config(tmp_path, out[out.index("phi_tags:"):])

    assert set(loaded) == {"0008,0090"}


def test_nothing_to_suggest_prints_no_empty_fragment(capsys):
    """Only tagless findings: no rule resolves them, so no `phi_tags:` key
    at all -- an empty one parses as `null`, which is not a mapping."""
    _report_phi_findings([_finding(None, "PixelData[Frame=0]")])
    out = capsys.readouterr().out

    assert "phi_tags:" not in out, out
    assert "1 finding(s)" in out, out

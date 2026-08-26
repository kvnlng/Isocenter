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

from isocenter.config_manager import ConfigLoader
from isocenter.session import _print_suggested_config


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

    loaded = ConfigLoader.load_phi_config(str(path))

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
    else `unknown_tag`, while `phi_tags.json` already named more."""
    parsed = yaml.safe_load(_emit(capsys))

    assert parsed["phi_tags"]["0010,0010"]["name"] == "Patient Name"
    assert parsed["phi_tags"]["0008,0020"]["name"] == "Study Date"


def test_an_unrecognised_tag_still_produces_a_usable_rule(capsys):
    """A private vendor tag has no name to look up, and the fragment must
    stay loadable rather than omitting it."""
    parsed = yaml.safe_load(_emit(capsys))

    rule = parsed["phi_tags"]["0009,1001"]
    assert rule["action"] == "REMOVE"
    assert rule["name"]

"""Every redaction zone the documentation shows is one the loader accepts (#424).

Zones are `[y1, y2, x1, x2]` -- row start, row end, column start, column
end -- everywhere they are read: `ConfigLoader._validate_rule`,
`RedactionVerifier._coverage`, both redact paths and the export worker
(#258, #264). OCR boxes are `[x, y, w, h]`, and the two spaces are easy
to confuse because both are four integers. Until #424, `docs/ocr.md`
labelled its zones `[x, y, width, height]` and gave box values under
that label, and `docs/configuration.md` handed `discover_redaction_zones()`'s
`DiscoveryResult` to `add_rule(zones=...)` whole. Loading either ocr.md
example as published printed `Invalid ROI logic (Start > End)` and left
the session with no rules at all.

**These tests execute the claim rather than grep for its wording.** A
test that looked for the corrected text would pin today's sentence, and
would pass just as happily if the numbers under it were wrong. Z1 and Z3
run what the page says through the loader's own validation, so a wrong
page is red and the wording can change freely. Z2 is the one wording
check, and it pins the *absence* of a false label, because a zone like
`[0, 100, 0, 200]` is valid read either way and Z1 alone cannot see a
box-space label sitting over it.

This file imports only `config_manager`, `configuration` and
`discovery`, all in `scripts/mutation_probe.py`'s `NOT_PROBED`, so it
needs no `TARGETS` row (`tests/test_mutation_probe_targets._importers`
matches the text of a dotted module name anywhere in a test file).
"""
import pathlib
import re
from types import SimpleNamespace

import yaml

from isocenter.config_manager import ConfigLoader
from isocenter.configuration import IsocenterConfiguration
from isocenter.discovery import DiscoveryCandidate, DiscoveryResult

REPO = pathlib.Path(__file__).resolve().parent.parent

# Fences under this prefix are dated design records, not documentation,
# and are deliberately never rewritten -- the same exclusion
# `tests/test_documented_api_exists.py` makes.
_EXCLUDED_DOCS = "docs/superpowers/"

_FENCE = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)

#: Measured when #424 landed: README.md, docs/configuration.md (two) and
#: docs/ocr.md (two). A floor, not an exact count, so a new example is
#: welcome; but a walk that finds nothing is a broken check that passes,
#: which is #299's lesson.
_MIN_ZONE_FENCES = 5


def _documentation_files():
    files = [REPO / "README.md"]
    for path in sorted((REPO / "docs").rglob("*.md")):
        if _EXCLUDED_DOCS in path.relative_to(REPO).as_posix():
            continue
        files.append(path)
    return [path for path in files if path.is_file()]


def _yaml_zone_fences():
    """`(where, body)` for every ```` ```yaml ```` fence that names zones."""
    found = []
    for path in _documentation_files():
        text = path.read_text(encoding="utf-8")
        for match in _FENCE.finditer(text):
            lang, body = match.group(1), match.group(2)
            if lang == "yaml" and "redaction_zones" in body:
                line = text.count("\n", 0, match.start()) + 1
                found.append((f"{path.relative_to(REPO)}:{line}", body))
    return found


def test_every_documented_redaction_zone_passes_the_loaders_validation():
    """Z1: the loader's own rule is the oracle, not a local idea of "valid".

    Red before #424 on both `docs/ocr.md` fences, with the message a user
    following the page saw: `Invalid ROI logic (Start > End)`.
    """
    fences = _yaml_zone_fences()
    assert len(fences) >= _MIN_ZONE_FENCES, (
        f"found {len(fences)} yaml fences naming redaction_zones, expected "
        f"at least {_MIN_ZONE_FENCES}; the walk is not seeing the pages")

    refused = []
    for where, body in fences:
        document = yaml.safe_load(body)
        assert isinstance(document, dict) and "machines" in document, (
            f"{where}: a redaction_zones fence with no `machines` list is "
            "not a config this check can validate")
        for index, rule in enumerate(document["machines"]):
            try:
                ConfigLoader._validate_rule(rule, index)  # pylint: disable=protected-access
            except ValueError as exc:
                refused.append(f"{where}: {exc}")
    assert not refused, (
        "the configuration loader refuses these documented zones:\n    "
        + "\n    ".join(refused))


def test_no_documented_zone_list_is_labelled_in_box_space():
    """Z2: no comment inside a zone fence describes a zone as `[x, y, w, h]`."""
    fences = _yaml_zone_fences()
    assert len(fences) >= _MIN_ZONE_FENCES
    box_words = re.compile(r"\bwidth\b|\bheight\b|\bx,\s*y\b", re.IGNORECASE)
    mislabelled = []
    for where, body in fences:
        for line in body.splitlines():
            if "#" not in line:
                continue
            comment = line.split("#", 1)[1]
            if box_words.search(comment):
                mislabelled.append(f"{where}: {line.strip()}")
    assert not mislabelled, (
        "a zone is [y1, y2, x1, x2]; these comments label one in OCR box "
        "space [x, y, w, h]:\n    " + "\n    ".join(mislabelled))


def _python_fence_under(path, heading):
    text = path.read_text(encoding="utf-8")
    start = text.find(f"\n{heading}\n")
    assert start != -1, f"{path.name} has no {heading!r} heading"
    match = re.compile(r"```python\n(.*?)```", re.DOTALL).search(text, start)
    assert match is not None, f"no python fence under {heading!r} in {path.name}"
    next_heading = text.find("\n## ", start + 1)
    assert next_heading == -1 or match.start() < next_heading, (
        f"the first python fence after {heading!r} belongs to a later section")
    return match.group(1)


def test_the_configuration_page_discovery_example_stores_zones():
    """Z3: run configuration.md's discovery example and read what it stored.

    `discover_redaction_zones()` is stubbed to return a real
    `DiscoveryResult` holding one candidate box `[20, 50, 200, 30]`
    (`[x, y, w, h]`) seen in each of three sources, so `to_zones()` groups
    it into one zone. Measured: `to_zones()`'s own output for that box is
    `[50, 80, 20, 220]`. Before #424 the example stored the
    `DiscoveryResult` itself as the rule's zones, which the loader
    refuses with `'redaction_zones' must be a list`.
    """
    source = _python_fence_under(
        REPO / "docs" / "configuration.md", "## Auto-Discovery of Redaction Zones")

    def discover(*_args, **_kwargs):
        return DiscoveryResult(
            [DiscoveryCandidate("SMITH^JOHN", 95.0, [20, 50, 200, 30], k, "NAME_PATTERN")
             for k in range(3)], 3)

    configuration = IsocenterConfiguration()
    # `add_rule` writes only under `auto_save`, with a `config_path`
    # (#715). Neither is set, so the example changes memory only and
    # writes nothing to disk; asserted rather than assumed, so a future
    # default cannot turn this test into a stray file.
    assert configuration.config_path is None and not configuration.auto_save
    session = SimpleNamespace(discover_redaction_zones=discover,
                              configuration=configuration)

    exec(compile(source, "docs/configuration.md", "exec"),  # pylint: disable=exec-used
         {"session": session, "print": lambda *a, **k: None})

    assert configuration.rules, "the example stored no rule"
    for index, rule in enumerate(configuration.rules):
        ConfigLoader._validate_rule(rule, index)  # pylint: disable=protected-access
    assert [rule["redaction_zones"] for rule in configuration.rules] == [[[50, 80, 20, 220]]]

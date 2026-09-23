"""
Configuration manager for handling Isocenter system settings.

This module provides functionality to load, validate, and manage configuration
files for the Isocenter application: the unified YAML configuration
(schema version 2, checked key by key and type by type since #711-#713),
external privacy profile files, and the built-in profiles.
"""

import os
import logging
import copy
import contextlib
import difflib
from typing import Dict, Any, List, Optional
import re
import yaml

from .profiles import FLOOR, FLOOR_POLICY, PRIVACY_PROFILES, PROFILE_ALIASES

#: The schema version both writers stamp (`IsocenterConfiguration.save()`
#: and `Session.create_config()`, which read it at call time rather than
#: carrying their own literal). It is the schema's number, not the
#: package's, and has been "2.0" since December 2025 (#711). It never
#: bumps the major in 1.x. A 1.x bumps the minor for either of two
#: reasons, and adds a row to
#: `tests/test_config_schema_version.py::SCHEMA_BY_VERSION` for each:
#:
#: - it adds a key or a value;
#: - it applies an unchanged file differently, for example a value-less
#:   REPLACE now writing its VR's dummy (#556). Owner's ruling on #762.
#:
#: A 2.x file still loads unchanged under every 2.x.
#:
#: **Why the second reason matters.** The v1 policy fingerprint
#: (`configuration._canonical_policy_v1`) hashes this value, read at call
#: time. The bump is therefore the only thing that makes statuses scanned
#: under an older minor read as another policy. `export()` then says so
#: and the report grades REVIEW_REQUIRED until `audit()` runs again
#: (#555). Without the bump, the fingerprint equates two scans that
#: behave differently. Nothing checks that such a change bumps (#782).
#:
#: **The working test** is `docs/api/stability.md`'s promise. Bump
#: whenever the same file would be applied differently to the same input:
#: findings, values a rule writes, tags a rule reaches, pixel zones or
#: date jitter. A re-audit does not re-check pixel zones or jitter, but the
#: fingerprint still moves, because the version is in it. Every bump moves
#: every fingerprint, so it re-measures the (0012,0063) literals in
#: `tests/test_an_export_says_how_it_was_de_identified.py` and retakes
#: `fingerprint/output.json`.
CONFIG_VERSION = "2.0"

#: The one major this library reads. A string, compared as a string, so
#: `"02.0"` is not quietly read as 2.
_READABLE_MAJOR = "2"

#: What a file with no `version` line means: 2.0, permanently. Every
#: configuration in the documentation omits the line, so refusing an
#: absent version would refuse every file copied from it. Pinned rather
#: than "whatever this library reads", so a library that ever reads a
#: major 3 still reads an unversioned file as 2.
_UNVERSIONED_MEANS = "2.0"

# The schema (#712). Every key a configuration may carry, by level; a key
# outside these is refused by name rather than ignored. Before #712 the
# loader read these and silently ignored every other key, so a misspelt
# `remove_private_tag: false` loaded and the private tags the file asked
# to keep were removed. Extending the schema is one edit here, plus the
# minor bump `CONFIG_VERSION`'s comment describes.
_TOP_LEVEL_KEYS = frozenset({"version", "privacy_profile", "phi_tags",
                             "date_jitter", "remove_private_tags", "machines"})
#: `comment` is the shipped knowledge bases' own field
#: (`resources/redaction_rules.json`, `ctp_rules.json`) and what
#: `create_config` and `save()` write; `manufacturer` is what `add_rule`
#: writes. Both are metadata and nothing reads them, but refusing either
#: would refuse the library's own output.
_RULE_KEYS = frozenset({"serial_number", "manufacturer", "model_name",
                        "redaction_zones", "comment"})
#: `note` is written as data by `create_config` for every machine the
#: knowledge base matches by serial, so it is a zone key.
_ZONE_KEYS = frozenset({"roi", "note"})
#: `replacement` is deliberately absent and deliberately not reported as
#: unknown: it is the one known-refused key, and `_refused_phi_rule`
#: refuses it with its rename advice (#538).
_PHI_RULE_KEYS = frozenset({"name", "action", "value"})
#: An external profile file contributes its `phi_tags` and nothing else
#: (#712, owner ruling Q1): a profile carrying `privacy_profile: basic`
#: beside one rule loaded as that one rule.
_PROFILE_FILE_KEYS = frozenset({"version", "phi_tags"})

# Null is read as absent for the five optional metadata strings -- a rule's
# `manufacturer`, `model_name` and `comment`, a zone's `note`, a phi rule's
# `name` -- and only for them (review of #728, finding 1). 0.9.x auto-save
# wrote `manufacturer: null` and `model_name: null` for
# `add_rule(serial, eq.manufacturer, eq.model_name)` on equipment without
# those tags (both are None then), and `comment: null` after
# `update_rule(serial, {"comment": None})`; refusing null would refuse a file
# the library itself wrote. Nothing reads any of the five, so an absent value
# and a null one mean the same thing. This is not the rule for
# `remove_private_tags`, whose null is refused because it read as the
# opposite of the default, nor for `serial_number`, whose null is missing.
# The three checks that apply it say "null is absent" and point here.

#: `MAJOR.MINOR`, each a non-negative integer with no leading zero (#730).
#: `"2.00"` loaded as 2.0 under a looser `[0-9]+\.[0-9]+`, and `"02.0"` was
#: refused as a major this library does not read, which was the wrong
#: reason. One schema version has one spelling, as one profile has.
_VERSION_SHAPE = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
#: The looser shape, kept only to tell a leading zero from a non-version.
_VERSION_DIGITS = re.compile(r"[0-9]+\.[0-9]+")


def _declared_version(data: Dict[Any, Any], source: str) -> str:
    """The schema version `data` declares, or a `ValueError` (#711).

    Absent means `_UNVERSIONED_MEANS`. Present, it must be a `str` of the
    form `MAJOR.MINOR` whose major is `_READABLE_MAJOR`. An unquoted
    number is refused rather than coerced -- YAML reads `version: 2.10`
    as the float 2.1, the octal-serial trap again -- and so is `null`:
    only an absent line means "unversioned", and a bare `version:` is a
    typo, not a choice. A leading zero (`"2.00"`, `"02.0"`) is refused as
    a spelling (#730). `"1.0"` is refused too: it is the label the
    machines-only rules file carried before 2.0 (December 2025), its
    content loads as 2.0, and one schema has one spelling.
    """
    if "version" not in data:
        return _UNVERSIONED_MEANS
    version = data["version"]
    if not isinstance(version, str):
        raise ValueError(
            f"{source}: version must be a quoted string such as '2.0', got "
            f"{version!r} ({type(version).__name__}); unquoted, YAML reads "
            f"a version as a number, and 2.10 as the number 2.1 (#711)")
    if _VERSION_DIGITS.fullmatch(version) and not _VERSION_SHAPE.fullmatch(version):
        raise ValueError(
            f"{source}: version {version!r} is not canonical: write "
            f"'MAJOR.MINOR' with no leading zero, such as '2.0' (#730)")
    if not _VERSION_SHAPE.fullmatch(version):
        raise ValueError(
            f"{source}: version {version!r} is not a 'MAJOR.MINOR' string "
            f"such as '2.0' (#711)")
    if version.split(".")[0] != _READABLE_MAJOR:
        raise ValueError(
            f"{source}: version {version!r} is a configuration schema this "
            f"isocenter does not read; it reads version {_READABLE_MAJOR} "
            f"({_READABLE_MAJOR}.0 through any {_READABLE_MAJOR}.x) (#711)")
    return version


def _newer_minor_note(declared: str, source: str) -> str:
    """The sentence a refusal gains when the file at `source` declares a
    minor newer than `CONFIG_VERSION`, else "" (#711).

    Any minor of the readable major loads. A newer minor that only changed
    how a file is applied (#762) loads with no note, is applied as this
    library applies it, and nothing flags it (#784 asks whether it should).
    A key or value this library lacks is refused, and this says why. Minors
    compare as integers: "2.10" is newer than "2.9". Read at call time,
    not import time, so the constant has one home.
    """
    ours = CONFIG_VERSION
    if int(declared.split(".")[1]) <= int(ours.split(".")[1]):
        return ""
    return (f"; {source} declares version {declared}; this isocenter reads "
            f"{ours}, so a key or value added after {ours} needs a newer "
            f"isocenter")


#: The attribute a `ValueError` carries once the innermost file's
#: `_noting_a_newer_minor` has judged it: the path of that file.
_JUDGED_BY = "_isocenter_version_judged_by"


@contextlib.contextmanager
def _noting_a_newer_minor(declared: str, source: str):
    """Re-raise any `ValueError` inside with `_newer_minor_note` appended.

    On every refusal, not only an unknown key: a newer minor may add a
    value to an existing key (a new action, a new profile name) or a key
    inside a rule, and each of those reaches this library as a different
    refusal. With no note to add, the original exception propagates
    untouched.

    Only the innermost file judges a refusal (review of #728). An
    external profile's refusal passes through the configuration's own
    wrap on its way out, and the configuration's version says nothing
    about the profile file: with no path in the note the configuration's
    `2.5` was blamed on a profile that declared nothing, and a `2.7`
    profile inside a `2.5` configuration got both notes. So the note
    names its file, and an exception an inner wrap has judged -- noted
    or not -- is marked and passed through unchanged by every outer one.
    """
    try:
        yield
    except ValueError as exc:
        if getattr(exc, _JUDGED_BY, None) is not None:
            raise
        note = _newer_minor_note(declared, source)
        if not note:
            setattr(exc, _JUDGED_BY, source)
            raise
        noted = ValueError(f"{exc}{note}")
        setattr(noted, _JUDGED_BY, source)
        raise noted from exc


def _unknown_keys(keys, known, where: str, whose: str,
                  refused_elsewhere=frozenset()) -> Optional[str]:
    """The refusal for `keys` outside `known`, or None (#712).

    Names **every** unknown key (sorted by `str`, since YAML allows
    `2:` and `yes:` as keys), offers `difflib`'s closest known key where
    it has one, and always lists the known keys. `where` follows the key
    list ("at the top level", or ""); `whose` names the level's keys ("A
    machine rule's"). `refused_elsewhere` is keys a later check refuses
    with better words -- only `replacement` in a phi rule (#538) -- and is
    passed by that one caller alone, so no other level exempts it.
    """
    unknown = sorted((k for k in keys if k not in known and k not in refused_elsewhere),
                     key=str)
    if not unknown:
        return None
    noun = "key" if len(unknown) == 1 else "keys"
    message = f"unknown {noun} {', '.join(repr(k) for k in unknown)}{where}"
    guesses = []
    for key in unknown:
        if isinstance(key, str):
            match = difflib.get_close_matches(key, sorted(known), n=1)
            if match:
                guesses.append((key, match[0]))
    listed = f"{whose} keys are {', '.join(sorted(known))} (#712)"
    if len(unknown) == 1 and guesses:
        return f"{message}; did you mean {guesses[0][1]!r}? {listed}"
    if guesses:
        meant = ", ".join(f"{guess!r} for {key!r}" for key, guess in guesses)
        return f"{message}; did you mean {meant}? {listed}"
    return f"{message}; {listed}"


def _checked_top_level(data: Dict[Any, Any], source: str) -> str:
    """Check a configuration's version, then its top-level keys; return
    the declared version (#711, #712).

    The version first: a file written for another major may carry keys
    this library has never heard of, and the version is the true reason
    to refuse it. `machine_rules` gets its own message: it was an alias
    `load_config` read as `machines` (and silently dropped when both were
    present), never documented, and a deleted spelling is named rather
    than reported as a typo -- the `replacement` -> `value` precedent
    (#538).
    """
    declared = _declared_version(data, source)
    with _noting_a_newer_minor(declared, source):
        if "machine_rules" in data:
            raise ValueError(
                f"{source}: 'machine_rules' is an old spelling of 'machines'; "
                f"rename it (#712)")
        reason = _unknown_keys(data, _TOP_LEVEL_KEYS, " at the top level",
                               "A version 2 configuration's")
        if reason is not None:
            raise ValueError(f"{source}: {reason}")
    return declared

#: Where this package's own shipped resources live.
#:
#: Hoisted out of `load_phi_config`'s body in #388 so a test could
#: monkeypatch it. That loader is gone (#729), and before it went it no
#: longer read a resource -- the default PHI policy is
#: `profiles.FLOOR_POLICY`, in Python, since #495 deleted
#: `resources/phi_tags.json` -- and the constant stays because
#: `publish.yml`'s wheel gate passes it to `require_package_resource` from
#: an installed wheel, where it is the only spelling of "this package's
#: resources directory" that does not point back at the source tree.
RESOURCES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "resources")

# There is deliberately no `load_dotenv()` at import (#543). It searched
# upward from this file's directory -- from the cwd only under `python -c`
# or a REPL -- so whether a project's `.env` applied depended on where the
# virtual environment lived, and importing a library changed the process
# environment. A caller who wants a `.env` loads it before importing.
# `tests/test_import_does_not_read_a_dotenv.py` holds this.


def require_package_resource(directory: str, basename: str,
                             consequence: str) -> str:
    """The path to a resource this package ships, or a refusal.

    Three loaders returned an empty collection when a shipped file was
    absent, with no log line and no audit row: the run then scanned every
    frame with no redaction rules, or audited against an empty PHI tag
    list, and reported clean (#388).

    **A refusal rather than a warning**, on #400's reasoning: a warning in
    front of a run that then succeeds is a line nobody reads, and there is
    nothing to *annotate*, because a missing shipped resource is never a
    correct state. The degrade-gracefully rule is about the optional
    extras (`ocr`, `nlp`, `docs`); a shipped package resource is the
    opposite kind of thing -- `setup.py`'s `package_data` promises it and
    `publish.yml` refuses to release a wheel without it, so this is the
    runtime half of a promise CI already makes. No audit row is written
    either, for the same reason.

    **`RuntimeError`, and deliberately not `FileNotFoundError`.**
    `ConfigLoader._load_yaml` already raises that for a *user's* config
    file, which is a different failure with a different remedy, and a
    caller writing `except FileNotFoundError` around `load_config` would
    silently swallow "your install is broken". The sharper reason is that
    the callers' own handlers are `except (OSError, ...)` and
    `FileNotFoundError` **is** an `OSError`: a refusal of that type, if it
    ever drifted inside one of those `try` blocks, would be caught and
    turned straight back into the empty collection this function exists to
    replace. Call it **before** the `try`.

    `directory` is a parameter rather than a module global read in here.
    Both callers' `RESOURCES_DIR` is what tests monkeypatch, and a helper
    that closed over its own copy would make every such test pass against
    the real source tree.

    `consequence` is the caller's own words for what continuing would have
    done. A generic sentence would be the same failure as a generic loss
    row: accurate, not generic, is the standard this applies to refusals
    as much as to anything else.

    Args:
        directory (str): the resources directory to look in.
        basename (str): the file's name, passed as a bare literal by every
            caller so `test_every_shipped_resource_is_named_by_the_package`
            can still see it in the AST.
        consequence (str): what a silent continue would have done, e.g.
            "scanned every frame with no machine redaction rules".

    Returns:
        str: the resolved path, which exists.

    Raises:
        RuntimeError: if the resource is not there.
    """
    path = os.path.join(directory, basename)
    if not os.path.exists(path):
        raise RuntimeError(
            f"Isocenter's shipped resource {basename} is missing from this "
            f"installation (looked in {path}). setup.py packages it and "
            f"publish.yml refuses to release a wheel without it, so its "
            f"absence is a broken install rather than a configuration "
            f"choice -- reinstall isocenter. Continuing would have "
            f"{consequence}, and reported a clean run.")
    return path


def get_logger() -> logging.Logger:
    """
    Retrieves the configured logger for the Isocenter application.

    Returns:
        logging.Logger: The 'isocenter' logger instance.
    """
    return logging.getLogger("isocenter")


def _lowercase_tag_keys(tags: Dict[Any, Any]) -> Dict[Any, Any]:
    """`tags` with every string key lowercased, order kept."""
    return {(key.lower() if isinstance(key, str) else key): value
            for key, value in tags.items()}


def _names_no_profile(profile_name: Any) -> bool:
    """True for `privacy_profile: none`.

    Not for `privacy_profile: null` (a bare `privacy_profile:` line), which
    is absent and means the floor (#730). It meant `none` until 1.0, so a
    template's blank left unfilled switched off every rule of the floor --
    the opposite of what leaving the line out does."""
    return isinstance(profile_name, str) and profile_name.strip().lower() == "none"


#: The actions `PhiInspector._scan_instance` dispatches on (`REPLACE` is
#: its `else` arm). Anything else was scanned as REPLACE without a word
#: until #456, so `action: OBLITERATE` loaded, printed "Configuration
#: Loaded", and replaced the value with `ANONYMIZED`.
_PHI_ACTIONS = frozenset({"KEEP", "REMOVE", "EMPTY", "REPLACE", "SHIFT", "JITTER"})


_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _is_tag_key(key: str) -> bool:
    """True for a `gggg,eeee` key: four hex digits, a comma, four more.

    The spelling every tag table here uses, and the only one the scan
    looks up. `'8,80'` (for 0008,0080) or a keyword such as `PatientName`
    loaded before #456 and matched nothing, so the rule never ran and
    nothing said so.
    """
    return (len(key) == 9 and key[4] == ","
            and all(ch in _HEX_DIGITS for ch in key[:4] + key[5:]))


#: PS3.15 Table E.1-1's own spellings of a repeating group (#556):
#: `50xx,xxxx` / `60xx,xxxx` for a whole group, `50xx,eeee` / `60xx,eeee`
#: for one element in every group. Matched on the lowercased key, so
#: either case of `x` is read.
_GROUP_MASK_KEY = re.compile(r"(50|60)xx,(xxxx|[0-9a-f]{4})")


def _is_group_mask_key(key: str) -> bool:
    """True for a repeating-group key (#556).

    Such a key names the **even** groups 5000-501E (retired Curve) or
    6000-601E (Overlay) and nothing else (PS3.5 7.6); the odd groups
    between them are private and belong to the `remove_private_tags`
    sweep. `privacy._rule_for` resolves a mask to the concrete tags it
    covers, the most specific key first. Until #556 no key could spell
    Table E.1-1's `Curve Data (50xx,xxxx)` row, so every curve element
    survived every profile.
    """
    return isinstance(key, str) and _GROUP_MASK_KEY.fullmatch(key.lower()) is not None


def _external_profile_tags(path: str) -> Dict[str, Any]:
    """The validated `phi_tags:` mapping of an external profile file.

    A file with no `phi_tags` key had its root mapping used as the tags
    (the fallback of `load_phi_config`, deleted in #729), so a profile written as a
    config -- `privacy_profile: basic` and its rules at the top level --
    loaded `privacy_profile` itself as a "tag" (review of #509). Refused,
    naming the file.

    The file's own `version` is checked as a configuration's is (#711),
    and any key but `phi_tags` and `version` is refused (#712, owner
    ruling Q1): a profile carrying `privacy_profile: basic` beside one
    rule loaded as that one rule, where its author plainly meant 621. The
    version first, as in a configuration; the no-`phi_tags` refusal
    before the key check, so a root mapping of tags keeps its own
    message rather than being told every tag is an unknown key.
    """
    data = ConfigLoader._load_yaml(path)
    if not isinstance(data, dict):
        raise ValueError(
            f"{path}: an external privacy profile must carry its rules under "
            f"a 'phi_tags:' mapping; this file has no phi_tags key")
    declared = _declared_version(data, path)
    with _noting_a_newer_minor(declared, path):
        if "phi_tags" not in data:
            raise ValueError(
                f"{path}: an external privacy profile must carry its rules "
                f"under a 'phi_tags:' mapping; this file has no phi_tags key")
        unknown = sorted((k for k in data if k not in _PROFILE_FILE_KEYS), key=str)
        if unknown:
            raise ValueError(
                f"{path}: an external privacy profile contributes only its "
                f"phi_tags; unknown key(s) {', '.join(repr(k) for k in unknown)} "
                f"would be ignored (#712)")
        return _validated_phi_tags(data["phi_tags"], path)


def _phi_rule_shape_refused(tag: Any, rule: Dict[Any, Any]) -> Optional[str]:
    """Why a rule mapping's keys or `name` are not the schema's, or None.

    First on every door, ahead of the action and VR checks (#712): a key
    outside `name`, `action` and `value` was ignored, so `{actoin: KEEP}`
    left the action at REPLACE and the value the file asked to keep was
    replaced -- and on a DA tag `{actoin: JITTER}` defaulted to REPLACE
    and was refused by #560 for a value it never asked to write. `name`
    must be a string when present (#713); null is absent, as for the
    other optional metadata strings (see the comment above
    `_VERSION_SHAPE`). `replacement` is exempt here
    and refused by `_refused_phi_rule` with its #538 rename advice.
    """
    reason = _unknown_keys(rule, _PHI_RULE_KEYS, "", "A rule's",
                           refused_elsewhere=frozenset({"replacement"}))
    if reason is not None:
        return f"phi_tags[{tag!r}] has {reason}"
    if rule.get("name") is not None and not isinstance(rule["name"], str):
        return (f"phi_tags[{tag!r}] name must be a string, got "
                f"{type(rule['name']).__name__} (#713)")
    return None


def _validated_phi_tags(tags: Any, source: str) -> Dict[str, Any]:
    """`tags` as a lowercase-keyed mapping, or a `ValueError` naming the tag.

    A tag's value is a display name (a string, which leaves the action at
    REPLACE) or a rule mapping whose `action`, if present, is one the
    inspector implements. Each of the other shapes loaded silently before
    #456 and failed, or misbehaved, later: a list-shaped `phi_tags` broke
    the scan when it iterated the mapping, an int rule was read as a name, and an unknown
    action was scanned as REPLACE. `None` (a bare `phi_tags:` line) is the
    empty mapping it plainly means.
    """
    if tags is None:
        return {}
    if not isinstance(tags, dict):
        raise ValueError(
            f"{source}: 'phi_tags' must be a mapping of tag to rule, got "
            f"{type(tags).__name__}")
    for tag, rule in tags.items():
        if not isinstance(tag, str):
            raise ValueError(
                f"{source}: phi_tags key {tag!r} must be a quoted "
                f"'gggg,eeee' string, got {type(tag).__name__}")
        if not (_is_tag_key(tag) or _is_group_mask_key(tag)):
            raise ValueError(
                f"{source}: phi_tags key {tag!r} is not a 'gggg,eeee' tag "
                f"(four hex digits, a comma, four hex digits, such as "
                f"'0010,0010'), or a repeating-group key such as "
                f"'60xx,xxxx'; the scan reads no tag by that key, so the "
                f"rule would never run")
        if isinstance(rule, dict):
            # Here as well as in `_refused_phi_rule`: that one judges the
            # merged policy, where a configuration's rule for a tag
            # replaces an external profile's, so a typo in the profile's
            # rule would never be seen.
            reason = _phi_rule_shape_refused(tag, rule)
            if reason is not None:
                raise ValueError(f"{source}: {reason}")
            action = rule.get("action", "REPLACE")
            if not isinstance(action, str) or action.upper() not in _PHI_ACTIONS:
                raise ValueError(
                    f"{source}: phi_tags[{tag!r}] has action {action!r}; the "
                    f"actions are {', '.join(sorted(_PHI_ACTIONS))}")
        elif not isinstance(rule, str):
            raise ValueError(
                f"{source}: phi_tags[{tag!r}] is {rule!r}; a tag's value is "
                f"either its display name (a string) or a rule mapping such "
                f"as {{action: REMOVE, name: ...}}")
    return _lowercase_tag_keys(tags)


#: The VRs whose value is not a string, so that no `value:` a config can
#: write fits them: the refusal's advice omits "give a value" for these.
_NON_STRING_VRS = frozenset({"OB", "OD", "OF", "OL", "OV", "OW", "UN",
                             "US", "SS", "UL", "SL", "UV", "SV", "FL", "FD",
                             "AT"})

#: What a `REPLACE` with no `value:` writes on a standard tag, by the
#: tag's dictionary VR (#557). PS3.15 Table E.1-1a's D is "replace with a
#: non-zero length value that may be a dummy value and consistent with the
#: VR"; `basic@2026c` maps D and every code with a D arm to that REPLACE.
#:
#: - Text: `ANONYMIZED`, the constant REPLACE always wrote, so no text
#:   export changes. Ten characters, under every limit (16 for AE, CS,
#:   SH), and valid in each of these VRs (a relative reference for UR).
#: - DA and DT `19000101` (a DT may stop at the date: no time of day is
#:   invented, see `exporters/wfdb.py::_real_timing`), TM `000000`, AS
#:   `000D`: visibly placeholder values, not plausible real ones.
#: - Binary: zero bytes, one word of the VR's width, so the value field is
#:   even (PS3.5 7.1.1).
#:
#: A VR not here has no dummy (`_vr_dummy` is None). A value-less REPLACE
#: on UI is the keyed UID replacement instead (#544; `privacy.
#: _replacement_uid_for`), never a constant: one dummy UID would merge
#: every study under it. On the rest it is refused: numeric VRs and AT (no
#: Table E.1-1 D row is either, and a zero reads as a plausible value), SQ
#: (no dummy item is valid independent of the IOD; the scan warns and
#: applies nothing), and a compound dictionary VR.
#: Changing a value here changes `basic@2026c`'s output, which is frozen
#: from the v1.0.0 tag (#714).
VR_DUMMY = {
    **{vr: "ANONYMIZED" for vr in ("AE", "CS", "LO", "LT", "PN", "SH", "ST",
                                   "UC", "UT", "UR")},
    "DA": "19000101",
    "DT": "19000101",
    "TM": "000000",
    "AS": "000D",
    "OB": b"\x00\x00",
    "UN": b"\x00\x00",
    "OW": b"\x00\x00",
    "OF": b"\x00" * 4,
    "OL": b"\x00" * 4,
    "OD": b"\x00" * 8,
    "OV": b"\x00" * 8,
}


def _vr_dummy(tag: str) -> Any:
    """The dummy a value-less REPLACE writes on `tag` (`VR_DUMMY`), or None
    when its dictionary VR has none, and for a private, unknown or
    malformed tag (#557).

    The dictionary VR and never the recorded one, so the value a rule
    writes does not depend on the file it meets; a private or unknown tag
    keeps `ANONYMIZED` and the exporter's LO fallback (#571). One value,
    whatever the tag's multiplicity: every D-arm row of Table E.1-1 is VM
    1 or 1-n."""
    return VR_DUMMY.get(_standard_dictionary_vr(tag))


#: The three tags the Patient and the Study own (#537). Their rules are
#: read by `privacy._owned_rule`; the validator knows two things about
#: them: Patient ID cannot be emptied, removed, shifted or given a
#: literal, and Study Date's REPLACE with no value is the shift.
_PATIENT_ID = "0010,0020"
_STUDY_DATE = "0008,0020"


def _standard_dictionary_vr(tag: str) -> Optional[str]:
    """The dictionary VR of a standard (even-group) tag, or None for a
    private tag, an unknown one, and a key that is not a tag.

    No parity test: pydicom's standard dictionary, repeaters included,
    holds no odd-group entry (measured, pydicom 3.0.2), so a private tag
    is a `KeyError` like any unknown one. A parity test here was an
    equivalent mutant."""
    # Local: pydicom is wanted only when a rule is checked, and this
    # module is imported by everything that reads a config.
    from pydicom.datadict import dictionary_VR  # pylint: disable=import-outside-toplevel
    try:
        number = int(tag.replace(",", ""), 16)
    except (AttributeError, ValueError):
        return None
    try:
        return str(dictionary_VR(number))
    except KeyError:
        return None


def _dictionary_vm(tag: str) -> Optional[str]:
    """The dictionary value multiplicity of a standard tag (`'1'`,
    `'1-n'`, ...), or None where `_standard_dictionary_vr` is None."""
    from pydicom.datadict import dictionary_VM  # pylint: disable=import-outside-toplevel
    if _standard_dictionary_vr(tag) is None:
        return None
    return str(dictionary_VM(int(tag.replace(",", ""), 16)))


def _vm_allows(vm: str, count: int) -> bool:
    """Whether `count` values meet a dictionary VM (review of #574 round 2,
    P-4). pydicom's dictionary spells a VM four ways: `'N'`, `'N-M'`,
    `'N-n'`, `'N-Nn'` (`'2-2n'`: 2, 4, 6, ...). A spelling outside those
    allows any count, so a dictionary refresh cannot refuse a rule by an
    unparsed VM."""
    lower, _, upper = vm.partition("-")
    try:
        lower_n = int(lower)
        if not upper:
            return count == lower_n
        if upper == "n":
            return count >= lower_n
        if upper.endswith("n"):
            # Every `N-Nn` in the dictionary today has N equal to the step,
            # so the lower bound is implied by the step for any count of at
            # least one. It is kept for a spelling such as `4-2n`, which the
            # step alone would misread, and tested on that spelling directly.
            return count >= lower_n and count % int(upper[:-1]) == 0
        return lower_n <= count <= int(upper)
    except ValueError:
        return True


def _dt_is_a_range(value: str) -> bool:
    """True when a `-` in a DT string is not its UTC offset (review of #574
    round 2, P-3). The offset is `&ZZXX` at the end -- `+` or `-`, hours at
    most 14, minutes below 60 (PS3.5 Table 6.2-1) -- so one such suffix is
    set aside and any `-` left is a range: `20230101-20230201`,
    `-20230201`, `20230101-`, and `2023-2024`, whose 20 is no hour of an
    offset. `2023-1200` stays a year at offset -1200, which is what PS3.5
    reads it as."""
    match = re.search(r"[+-]([0-9]{2})([0-9]{2})$", value)
    if match and int(match.group(1)) <= 14 and int(match.group(2)) < 60:
        value = value[:match.start()]
    return "-" in value


def _is_oversized_tag_key(tag: str) -> bool:
    """True for a key that reads as hex but names no 32-bit tag, such as
    `'10000,0010'`. pydicom's `Tag` raises `OverflowError` for it, which
    is not the `ValueError` every other refusal is (review of #574)."""
    try:
        number = int(tag.replace(",", ""), 16)
    except ValueError:
        return False
    return not 0 <= number <= 0xFFFFFFFF


def _dictionary_vr_refuses(tag: str, value: Any) -> Optional[str]:
    """The dictionary VR of a **standard** tag when that VR cannot hold
    `value`, else None (#560).

    None as well for a private (odd-group) tag, an unknown tag, and a
    sequence: the exporter writes a private value its recorded VR cannot
    hold under one that holds it, with a WARNING row (#571), so a refusal
    there would keep an identifier the write removes; an unknown tag has
    no VR to judge by; and a value on a sequence is warned about by the
    scan, not written.

    The verdict is pydicom's `validate_value`, not `io_handlers._value_fits_vr`,
    which skips repertoire by design (a lower-case CS fits) and checks
    format only for DA, DT, TM, UI and AS. Two things `validate_value`
    does not do are done here: AT is refused outright (pydicom does not
    validate an AT string, and the value is a tag), and a compound
    dictionary VR (`US or SS`, `OB or OW`) is split, because pydicom has
    no validator under that name and passes anything. It is refused when
    no arm holds the value.

    On a tag whose multiplicity allows several values, a string is judged
    one `\\`-separated value at a time: `validate_value` reads the whole
    string as one value, so `A\\P` on Patient Orientation (CS, VM 2) was
    refused for the backslash CS's repertoire lacks. One value on a VM-1
    tag holding a backslash is `_refused_phi_rule`'s to refuse.
    """
    from pydicom import config as pydicom_config  # pylint: disable=import-outside-toplevel
    from pydicom.valuerep import validate_value  # pylint: disable=import-outside-toplevel

    vr = _standard_dictionary_vr(tag)
    if vr is None:
        return None
    # A sequence needs no arm of its own: pydicom has no validator for SQ
    # and passes any value, and the scan warns about a value on one.
    arms = [arm.strip() for arm in vr.split(" or ")]
    parts = [value]
    if isinstance(value, (list, tuple)):
        # A multi-valued write, one value at a time: the keyed UID
        # replacement of a VM 1-n element is a list (#544), and
        # `validate_value` reads a list as one value and refuses it.
        parts = list(value)
    elif isinstance(value, str) and _dictionary_vm(tag) != "1":
        parts = value.split("\\")

    def holds(arm):
        if arm == "AT":
            return False
        try:
            for part in parts:
                validate_value(arm, part, pydicom_config.RAISE)
        except (ValueError, TypeError):
            return False
        return True

    return None if any(holds(arm) for arm in arms) else vr


def _refused_phi_rule(tag: Any, rule: Any) -> Optional[str]:
    """Why this one rule cannot be honoured, or None (#537, #538, #559,
    #560). The shape checks -- a tag key, an action the inspector
    implements, a rule that is a string or a mapping -- are
    `_validated_phi_tags`' and are repeated only for the action, because
    `PhiInspector(config_tags=)` and `configuration.phi_tags` assigned
    directly reach the scan without the loader.

    The checks, in the order their messages are tested:

    0. A key outside `name`, `action` and `value`, and a `name` that is
       not a string (`_phi_rule_shape_refused`, #712/#713) -- first, so a
       misspelt `actoin:` is named rather than judged as the REPLACE it
       silently became.
    1. `replacement:` is 0.9.7's `set_phi_tag` spelling of `value:`,
       which nothing read (#538). One spelling, so it is refused by name.
    2. A `value:` under anything but REPLACE writes nothing.
    3. A `value:` that is not a string cannot be written as one.
    3a. A repeating-group key (`60xx,xxxx`, #556) takes REMOVE or KEEP:
       it names elements of many VRs, so an action whose effect depends
       on the VR could not say what it writes. Nothing after this reads a
       mask, which names no dictionary tag.
    4. Patient ID can only be kept or pseudonymised: the ID is what keeps
       two patients apart, and `anonymize()` merges patients that share
       one (#548), so an emptied or literal ID would merge every patient.
    5. SHIFT/JITTER on a standard tag that is not DA or DT declined on
       every pass (#559).
    6. REPLACE on a standard tag whose VR cannot hold what it writes
       (#560). Study Date's REPLACE with no value is the shift (#537, Q3)
       and is not judged as a literal. With no `value:` it writes the
       VR's dummy (`VR_DUMMY`, #557), and on UI the keyed UID replacement
       (#544), so it is refused only on a VR with neither: numeric VRs,
       AT and a compound VR. A `value:` a UI cannot hold is refused with
       advice naming the value-less spelling. Until #557 DA, DT, TM, AS
       and the binary VRs were refused here too, for the `ANONYMIZED`
       they could not hold; until #544, UI was.
    7. A REPLACE `value:` pydicom's `validate_value` passes and the tag
       still cannot hold (review of #574): a `-` in a DA or TM, which is
       a range, and a `\\` on a tag of multiplicity 1, which is a second
       value. In a DT a `-` is a range only where it is not the UTC offset
       at the end (`_dt_is_a_range`), and on any other tag the count of
       `\\`-separated values must meet the dictionary VM (`_vm_allows`;
       review of #574 round 2, P-3 and P-4). Only a `value:` is counted:
       REPLACE with no value writes one value (`ANONYMIZED` on text, as in
       0.9.7, or the VR's dummy), and no rule of that shape is refused by
       its count.

    Before all of them, a key naming no 32-bit tag is refused as the
    loader refuses a key that is not `gggg,eeee`; on the in-code doors it
    raised pydicom's `OverflowError` (review of #574).
    """
    if isinstance(tag, str) and _is_oversized_tag_key(tag):
        return (f"phi_tags key {tag!r} is not a 'gggg,eeee' tag (four hex "
                f"digits, a comma, four hex digits, such as '0010,0010'); the "
                f"scan reads no tag by that key, so the rule would never run")
    if isinstance(rule, dict):
        shape = _phi_rule_shape_refused(tag, rule)
        if shape is not None:
            return shape
        action = rule.get("action", "REPLACE")
        if not isinstance(action, str) or action.upper() not in _PHI_ACTIONS:
            return (f"phi_tags[{tag!r}] has action {action!r}; the actions "
                    f"are {', '.join(sorted(_PHI_ACTIONS))}")
        action = action.upper()
        value = rule.get("value")
        if "replacement" in rule:
            return (f"phi_tags[{tag!r}] has a 'replacement' key; the key is "
                    f"'value' (0.9.8, #538), so rename it")
        if value is not None and action != "REPLACE":
            return (f"phi_tags[{tag!r}] has a value under {action}; only "
                    f"REPLACE writes a value")
        if value is not None and not isinstance(value, str):
            return (f"phi_tags[{tag!r}] value must be a string, got "
                    f"{type(value).__name__}")
    elif isinstance(rule, str):
        action, value = "REPLACE", None
    else:
        return None

    if not isinstance(tag, str):
        return None
    if _is_group_mask_key(tag):
        # Here, not only in `_validated_phi_tags`: `set_phi_tag`,
        # `configuration.phi_tags` and `PhiInspector(config_tags=)` never
        # reach that one (L1 condition (a)). After the shape checks and
        # before anything reads the dictionary, which has no entry for a
        # mask (condition (c)).
        if action in ("REMOVE", "KEEP"):
            return None
        return (f"phi_tags[{tag!r}] is {action}; a repeating-group key (50xx "
                f"or 60xx) takes REMOVE or KEEP, because it names elements "
                f"of many VRs (#556)")
    if tag == _PATIENT_ID and (action in ("REMOVE", "EMPTY", "SHIFT", "JITTER")
                               or value):
        said = f" with value {value!r}" if value else ""
        return (f"phi_tags['{tag}'] is {action}{said}; Patient ID can only be "
                f"kept (KEEP) or replaced by its keyed pseudonym (REPLACE with "
                f"no value), because the ID is what keeps two patients apart "
                f"and anonymize() merges patients that share one (#537)")
    if action in ("SHIFT", "JITTER"):
        # A sequence is exempt as it is from REPLACE: the scan warns that
        # the action has no meaning there (#547) and applies nothing.
        vr = _standard_dictionary_vr(tag)
        if vr is not None and not {"DA", "DT", "SQ"} & set(vr.split(" or ")):
            return (f"phi_tags['{tag}'] is {action}, and {tag} is {vr}; "
                    f"SHIFT and JITTER move a date by the patient's offset "
                    f"and apply only to DA and DT (#559)")
        return None
    if action == "REPLACE" and not value and _standard_dictionary_vr(tag) == "UI":
        # The keyed UID replacement (#544), which a UI holds by
        # construction. Refused until 1.0 (#560), so no configuration that
        # loaded in 0.9.8 changes meaning.
        return None
    if action == "REPLACE" and not (tag == _STUDY_DATE and not value):
        # With no `value:`, the VR's dummy (#557): the same spelling as the
        # scan's, so the loader judges exactly what the scan will write.
        # Until #557 this was `value or "ANONYMIZED"`, and a value-less
        # REPLACE on a DA, TM, DT, AS or binary tag was refused here.
        written = value or _vr_dummy(tag) or "ANONYMIZED"
        vr = _dictionary_vr_refuses(tag, written)
        if vr is not None:
            advice = "EMPTY or REMOVE"
            # Only for a `value:`: a value-less REPLACE on a DA or DT
            # writes its dummy and is never refused here.
            if vr in ("DA", "DT"):
                advice += ", or JITTER to shift it"
            # By arm: `US or SS` is as numeric as `US` (review of #574).
            if not set(vr.split(" or ")) <= _NON_STRING_VRS:
                advice += f", or give a value: that is a valid {vr}"
            uid = ("; REPLACE with no value gives it this project's "
                   "replacement UID (#544)") if vr == "UI" else ""
            return (f"phi_tags['{tag}'] is REPLACE, which writes {written!r}, "
                    f"and {tag} is {vr}, which cannot hold it; use {advice} "
                    f"(#560){uid}")
        if value:
            vr = _standard_dictionary_vr(tag)
            if vr in ("DA", "TM") and "-" in value:
                return (f"phi_tags['{tag}'] is REPLACE, which writes "
                        f"{value!r}, and a '-' in a {vr} is a range, which "
                        f"{tag} cannot hold; give one {vr} value (#560)")
            if vr == "DT" and _dt_is_a_range(value):
                return (f"phi_tags['{tag}'] is REPLACE, which writes "
                        f"{value!r}, and a '-' in a DT anywhere but its UTC "
                        f"offset (&ZZXX at the end) is a range, which {tag} "
                        f"cannot hold; give one DT value (#560)")
            vm = _dictionary_vm(tag)
            if "\\" in value and vm == "1":
                return (f"phi_tags['{tag}'] is REPLACE, which writes "
                        f"{value!r}, and {tag} holds one value, which a '\\' "
                        f"would make two; give a value without one (#560)")
            count = value.count("\\") + 1
            if vm is not None and not _vm_allows(vm, count):
                return (f"phi_tags['{tag}'] is REPLACE, which writes "
                        f"{value!r}, and {tag} holds {vm} values (its "
                        f"dictionary VM), which {count} '\\'-separated values "
                        f"are not; give a value of that multiplicity (#560)")
    return None


def validate_phi_policy(tags: Dict[str, Any], source: str) -> None:
    """Raise `ValueError` naming `source` and the first rule in `tags` the
    pipeline cannot honour (see `_refused_phi_rule`); return otherwise.

    Called on every door a policy comes in by, before anything changes
    (#456): the merged policy a config file resolves to
    (`ConfigLoader.load_unified_config`, so `load_config` and
    `audit(config_path=)`), `set_phi_tag`, `audit()` over
    `configuration.phi_tags` before a project secret is minted, and
    `PhiInspector.__init__`. The merged policy and not each file: a
    user's KEEP over an external profile's REMOVE on Patient ID is an
    honourable policy, and the profile alone is not.
    """
    if not isinstance(tags, dict):
        return
    for tag, rule in tags.items():
        key = tag.lower() if isinstance(tag, str) else tag
        reason = _refused_phi_rule(key, rule)
        if reason is not None:
            raise ValueError(f"{source}: {reason}")


def load_unified_config(path: str) -> Dict[str, Any]:
    """
    Loads the unified configuration file (YAML).

    The root must be a mapping (a root-level list, the pre-0.5.2 format,
    is refused), its `version` one this library reads (#711) and its
    top-level keys the schema's (#712). Merges 'privacy_profile' if
    specified (built-in or external).

    Args:
        path (str): Path to the YAML configuration file.

    Returns:
        Dict[str, Any]: The loaded configuration dictionary.

    Raises:
        ValueError: If the file is not YAML, or fails any check above or
            in the `phi_tags` and profile validation below.
    """
    return _loaded_unified_config(path)[0]


def _loaded_unified_config(path: str):
    """`load_unified_config`'s body: the configuration and its policy base.

    The base is what the policy was built on -- a built-in profile's pinned
    name, an external profile's path, `profiles.FLOOR` for a file with no
    `privacy_profile` line, or None for `privacy_profile: none` (and for an
    external profile that contributed no rules). Returned beside the dict
    rather than stored in it, so the floor's sentinel never becomes a
    value of a configuration mapping (#714).
    """
    if not (path.endswith('.yaml') or path.endswith('.yml')):
        raise ValueError("Configuration file must be a YAML file (.yaml or .yml)")

    # Through `_load_yaml`, so a missing file is `FileNotFoundError` and a
    # syntax error is `ValueError("Invalid YAML format ...")`. This called
    # `yaml.safe_load` directly, so a syntax error escaped as
    # `yaml.parser.ParserError` -- which `load_config` then swallowed
    # along with everything else (#456).
    config = ConfigLoader._load_yaml(path)
    if not isinstance(config, dict):
        # An empty file (None), a scalar or a list at the root. Each of
        # these escaped as a TypeError or AttributeError from the first
        # `.get` below.
        raise ValueError(
            f"{path}: a configuration must be a YAML mapping at its root "
            f"(privacy_profile, phi_tags, machines, ...), got "
            f"{type(config).__name__}")

    # First after the root is known to be a mapping (#711): before the
    # keys, and before a profile file is opened, so a file this library
    # does not read is refused for that and nothing else.
    declared = _checked_top_level(config, path)
    with _noting_a_newer_minor(declared, path):
        return _resolved_policy(config, path)


def _unshipped_profile_refusal(profile_name: str, path: str) -> ValueError:
    """The refusal for a `privacy_profile` holding `@` that names no
    profile this version ships (#714)."""
    aliases = ", ".join(f"{alias!r} means {pinned}"
                        for alias, pinned in sorted(PROFILE_ALIASES.items()))
    return ValueError(
        f"{path}: privacy_profile {profile_name!r} is not a profile this "
        f"isocenter ships; it ships {', '.join(sorted(PRIVACY_PROFILES))} "
        f"({aliases}). A later PS3.15 edition arrives as a new name in a "
        f"newer isocenter (#714)")


def _resolved_policy(config: Dict[str, Any], path: str):
    """`config` with `phi_tags` validated and merged over its profile (or
    the floor), and the policy base (`_loaded_unified_config`); the body of
    `load_unified_config` after the top-level checks."""
    # Validated, and lowercased, before anything is merged. The profiles'
    # keys are lowercase (profiles.py's header comment), so a user's
    # `0008,103E` merged as spelled sat beside the profile's `0008,103e`
    # as a second rule for one tag: `PhiInspector` collapsed the pair at
    # scan time with the later entry winning by dict order, and the
    # report counted both (#495).
    config["phi_tags"] = _validated_phi_tags(config.get("phi_tags"), path)

    # A null `privacy_profile:` is an absent one: the floor (#730). Popped
    # here, so the floor branch below is the one place that decides it.
    if "privacy_profile" in config and config["privacy_profile"] is None:
        config.pop("privacy_profile")

    # `privacy_profile: none`: the file's `phi_tags` are the
    # whole policy, with no base beneath them. The scaffold's header has
    # told users to write this "for manual control" since v2.0, and it
    # warned "Unknown privacy profile" and loaded nothing until #495.
    if "privacy_profile" in config and _names_no_profile(config["privacy_profile"]):
        config.pop("privacy_profile")
        return config, None

    # No `privacy_profile` line: the floor policy beneath the file's tags
    # (#495). A loaded config extends or overrides
    # what a bare session applies rather than replacing it with its own
    # few tags -- otherwise a one-tag file switches the floor off by
    # accident, which the #495 measurement shows (`MODE=onetag`: Study ID
    # and Institution Name back in the export). `action: KEEP` opts one
    # tag out; `privacy_profile: none` opts out of the floor entirely.
    if "privacy_profile" not in config:
        floor = copy.deepcopy(FLOOR_POLICY)
        get_logger().info(
            "%s names no privacy_profile: applying the floor policy (%d rules) "
            "beneath its %d phi_tags. Write 'privacy_profile: none' to load "
            "only the file's own tags.", path, len(floor), len(config["phi_tags"]))
        floor.update(config["phi_tags"])
        config["phi_tags"] = floor
        return config, FLOOR

    # Merge Privacy Profile
    profile_name = config["privacy_profile"]
    # Exactly as spelled: no case-folding and no stripping, so `Basic` and
    # `basic@2026c ` are refused rather than read as a name they resemble.
    pinned = (PROFILE_ALIASES.get(profile_name, profile_name)
              if isinstance(profile_name, str) else None)

    # 1. Built-in profiles, by pinned name or by a bare alias (#714). The
    # configuration records the pinned name, not the spelling: it is the
    # table that ran, and `save()` writes it back.
    if pinned in PRIVACY_PROFILES:
        profile_rules = copy.deepcopy(PRIVACY_PROFILES[pinned])
        config["privacy_profile"] = pinned
        edition = pinned.partition("@")[2]
        meaning = (f"; {profile_name!r} means {pinned} in every 1.x"
                   if profile_name != pinned else "")
        get_logger().info(
            "Loaded built-in privacy profile '%s' (PS3.15 edition %s) with "
            "%d rules%s.", pinned, edition, len(profile_rules), meaning)

    # 2. Any other value holding `@` is a profile name this version does
    # not ship, and is never tried as a path. Checked before `isfile`, and
    # on `@` alone rather than a grammar: otherwise a file named
    # `basic@2027a` in the working directory turns a refused edition into
    # a silently loaded external profile, and a grammar narrower than `@`
    # reopens that for the next name it does not match (#714).
    elif isinstance(profile_name, str) and "@" in profile_name:
        raise _unshipped_profile_refusal(profile_name, path)

    # 3. External File (Custom Profile). Its failures propagate: this
    # logged "Failed to load custom profile" and carried on with no
    # profile, which is #456's silence one file further out.
    elif isinstance(profile_name, str) and os.path.isfile(profile_name):
        profile_rules = _external_profile_tags(profile_name)
        get_logger().info("Loaded custom privacy profile from '%s' with %d rules.", profile_name, len(profile_rules))

    else:
        # A misspelt profile is refused, not warned about and dropped
        # (#456): the drop loaded the file's own tags with no base
        # beneath them, a policy nobody wrote, behind a warning in
        # front of a run that then succeeded. `comprehensive`, which
        # README and docs/configuration.md offered, never existed and
        # took this path.
        known = sorted(set(PRIVACY_PROFILES) | set(PROFILE_ALIASES))
        raise ValueError(
            f"{path}: privacy_profile {profile_name!r} is neither a "
            f"built-in profile ({', '.join(known)}), "
            f"'none', nor an existing file")

    if not profile_rules:
        # An external profile with no tags contributed nothing; naming
        # it would let the compliance report describe protection that
        # never ran.
        config.pop("privacy_profile", None)

    # User rules override profile rules
    profile_rules.update(config["phi_tags"])
    config["phi_tags"] = profile_rules

    return config, config.get("privacy_profile")


def _policy_base_rules(privacy_profile: Optional[str], floor: bool) -> Dict[str, Any]:
    """The rules a saved file's `privacy_profile` line brings in, which
    `IsocenterConfiguration.save()` diffs `phi_tags` against (#715).

    The inverse of `_resolved_policy`: a built-in name through
    `PROFILE_ALIASES` to `PRIVACY_PROFILES` (a pinned name is its own
    alias target, so the table is the pinned one's, #738 review N8); a
    string naming a file, that file's `phi_tags` as the loader reads them;
    no profile and `floor`, `FLOOR_POLICY`; no profile otherwise (`none`,
    or an external profile that contributed nothing), no rules.

    An external profile is **re-read now**, not snapshotted at load: the
    next load reads the file as it is on disk, so a diff against the file
    as it is now is the one that reloads to memory when the profile has
    changed or dropped a rule since. A profile that has gone gets the
    refusal the loader gives a missing profile, which a snapshot would only
    defer to the next load; this one adds where a relative path was looked
    for, because under `auto_save` every change method raises it, and the
    usual cause is a `chdir` since the load (review of #742).

    The returned mapping is the module's own table for a built-in; callers
    read it and never write it.
    """
    if privacy_profile is None:
        return FLOOR_POLICY if floor else {}
    if _names_no_profile(privacy_profile):
        # Assigned in code: the loader leaves None for `none`.
        return {}
    if not isinstance(privacy_profile, str):
        raise ValueError(
            f"configuration.save(): privacy_profile must be a profile name or "
            f"a path, got {type(privacy_profile).__name__}")
    pinned = PROFILE_ALIASES.get(privacy_profile, privacy_profile)
    if pinned in PRIVACY_PROFILES:
        return PRIVACY_PROFILES[pinned]
    if "@" in privacy_profile:
        raise _unshipped_profile_refusal(privacy_profile, "configuration.save()")
    if os.path.isfile(privacy_profile):
        return _external_profile_tags(privacy_profile)
    known = ", ".join(sorted(set(PRIVACY_PROFILES) | set(PROFILE_ALIASES)))
    where = ("" if os.path.isabs(privacy_profile) else
             f"; a relative path is looked for in the working directory, "
             f"now {os.getcwd()}")
    raise ValueError(
        f"configuration.save(): privacy_profile {privacy_profile!r} is neither "
        f"a built-in profile ({known}), 'none', nor an existing file, so a "
        f"file naming it would not load{where}. If the profile file has "
        f"moved, set privacy_profile to its path (#715)")


class ConfigLoader:
    """
    Loads and validates configuration files for the Isocenter system.

    This class provides static methods to parse unified YAML configuration
    files (schema version 2). It handles configuration validation,
    normalization, and file I/O operations.

    One door reads a configuration file, `load_unified_config` (#729).
    `load_redaction_rules` read only `machines` and checked nothing at the
    top level; `load_phi_config` returned a file's own `phi_tags` with no
    profile merged and no `validate_phi_policy`. Each accepted files the
    other refused, and both were deleted before the 1.0 freeze.

    The class also provides utility methods for filename sanitization and YAML parsing.
    """

    @staticmethod
    def load_unified_config(
            filepath: str) -> tuple[Dict[str, Any], List[Dict[str, Any]],
                                    Dict[str, Any], bool, Optional[str]]:
        """
        Parses the unified YAML config (v2.0).

        Extracts the core configuration components: PHI tags, machine rules,
        date jitter settings, and global flags.

        Args:
            filepath (str): Path to the config file.

        Returns:
            tuple: (phi_tags, machine_rules, date_jitter_config,
            remove_private_tags, policy_base). The last element is what
            the policy was built on (#714): a built-in profile's pinned
            name (`basic@2026c`, also when the file said `basic`), an
            external profile's path, `profiles.FLOOR` -- an object,
            compared with `is` -- for a file with no `privacy_profile`
            line, or None for `privacy_profile: none` and for an external
            profile that contributed no rules.
        """
        # The version, the top-level keys, the phi_tags and the profile.
        data, base = _loaded_unified_config(filepath)
        # Already checked; read again only for the newer-minor note on the
        # refusals below, which a newer minor can reach too (a key inside
        # a rule, a new value).
        with _noting_a_newer_minor(_declared_version(data, filepath), filepath):
            return ConfigLoader._checked_parts(data, filepath, base)

    @staticmethod
    def _checked_parts(
            data: Dict[str, Any], filepath: str,
            base: Any) -> tuple[Dict[str, Any], List[Dict[str, Any]],
                                Dict[str, Any], bool, Any]:
        """The rest of `load_unified_config`: the merged policy judged, the
        machines, `date_jitter` and `remove_private_tags` checked."""
        # Everything below validates before it returns, and `load_config`
        # assigns only what this returns: a file that fails any check
        # leaves the session's configuration exactly as it was (#456).
        phi_tags = data["phi_tags"]
        # The merged policy, whichever branch produced it (a built-in or
        # external profile beneath the file, the floor, or `none`), so a
        # profile row the file did not override is judged and a file's
        # KEEP over a profile's refused row is not (#537, #560).
        validate_phi_policy(phi_tags, filepath)
        # `machines` only: the `machine_rules` alias read here until #712
        # is refused by name at the top level (`_checked_top_level`).
        machine_rules = data.get("machines", [])
        if machine_rules is None:
            machine_rules = []
        if not isinstance(machine_rules, list) or not all(
                isinstance(rule, dict) for rule in machine_rules):
            raise ValueError(
                f"{filepath}: 'machines' must be a list of rule mappings "
                f"(serial_number, redaction_zones, ...), got {machine_rules!r}")

        # Date Jitter. One shape is read, {min_days: int, max_days: int}.
        # Anything else loaded until #456 and then failed in `load_config`'s
        # own print, after the assignments, leaving `date_jitter: soon` in
        # the session. A bare int was a second spelling of equal bounds,
        # undocumented and never written by the library, and was deleted
        # before the 1.0 freeze (owner ruling Q2, #713). A null
        # `date_jitter:` is the default, unlike a null
        # `remove_private_tags:` below: an absent range has one obvious
        # meaning here, and the default is what it gets.
        dj = data.get("date_jitter")
        if dj is None:
            date_jitter_config = {"min_days": -365, "max_days": -1}
        elif isinstance(dj, int) and not isinstance(dj, bool):
            raise ValueError(
                f"{filepath}: 'date_jitter' must be {{min_days: int, "
                f"max_days: int}}; the single-int form was removed in 1.0 -- "
                f"write {{min_days: {dj}, max_days: {dj}}} for the same fixed "
                f"shift (#713)")
        elif (isinstance(dj, dict) and set(dj) == {"min_days", "max_days"}
              and all(isinstance(v, int) and not isinstance(v, bool)
                      for v in dj.values())):
            date_jitter_config = dj
        else:
            raise ValueError(
                f"{filepath}: 'date_jitter' must be {{min_days: int, "
                f"max_days: int}}, got {dj!r}")
        # Bounds the wrong way round have at least one of them wrong, and
        # the loader cannot know which (owner ruling Q3, #713). They loaded
        # until 1.0 and `RemediationService` swapped them silently; that
        # swap stays, for a range assigned in code, which no loader sees.
        if date_jitter_config["min_days"] > date_jitter_config["max_days"]:
            raise ValueError(
                f"{filepath}: 'date_jitter' min_days "
                f"{date_jitter_config['min_days']} is greater than max_days "
                f"{date_jitter_config['max_days']}; one of them is wrong, and "
                f"which cannot be told from the file (#713)")

        # A bool, and only a bool (#713). Absent is True, as ever. Present
        # and anything else is refused rather than read for truth:
        # `"false"` is a non-empty string, which read as true and removed
        # the private tags the file asked to keep; `0`/`1` are refused by
        # `isinstance(..., bool)`, not `int`. A bare `remove_private_tags:`
        # (null) is refused too, although `phi_tags:` and `machines:` read
        # null as empty: a bool has no empty, and null read as falsy --
        # keeping the private tags, the opposite of the default. Do not
        # "make the nulls consistent" with `date_jitter:` above.
        remove_private_tags = data.get("remove_private_tags", True)
        if not isinstance(remove_private_tags, bool):
            why = {
                str: (f"a quoted {remove_private_tags!r} is a non-empty "
                      f"string, which reads as true"),
                type(None): ("a bare 'remove_private_tags:' is null, which "
                             "reads as false and keeps the private tags, the "
                             "opposite of the default"),
            }.get(type(remove_private_tags),
                  "write true or false, unquoted")
            raise ValueError(
                f"{filepath}: 'remove_private_tags' must be true or false, got "
                f"{remove_private_tags!r} ({type(remove_private_tags).__name__}); "
                f"{why} (#713)")

        # Validate machines
        for i, rule in enumerate(machine_rules):
            ConfigLoader._validate_rule(rule, i)

        return (phi_tags, machine_rules, date_jitter_config,
                remove_private_tags, base)

    @staticmethod
    def clean_filename(filename: str) -> str:
        """
        Sanitizes a string to be safe for use as a filename.

        Replaces spaces with underscores and removes non-alphanumeric characters
        (except key delimiters like dash/dot).
        """
        # import re  <-- Removed

        s = str(filename).strip().replace(" ", "_")
        return re.sub(r'(?u)[^-\w.]', '', s)

    @staticmethod
    def _load_yaml(filepath: str) -> Dict[str, Any]:
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"Configuration file not found: {filepath}")

        try:
            with open(filepath, 'r', encoding="utf-8") as f:
                return yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ValueError(f"Invalid YAML format in {filepath}: {e}") from e

    @staticmethod
    def _validate_rule(rule: Dict[str, Any], index: int):
        """Raise `ValueError` for a machine rule the redaction cannot read
        as written; return otherwise.

        The one function every rule door calls: the loader, and
        `add_rule`/`update_rule` before they store (and, with auto-save on,
        write) a rule (#712). The order is load-bearing, and each
        step is pinned by a test:

        1. Unknown keys (#712), before the serial, so a misspelt
           `serial_numbr:` is named rather than reported as a missing
           serial.
        2. `serial_number`: present and non-empty, then a `str` (#713),
           then not blank (#730).
           An unquoted serial is a YAML number -- `0123` loads as the
           octal 83 -- and never equals a Device Serial Number, so the
           rule matched nothing and the machine was not redacted.
        3. `manufacturer`, `model_name` and `comment` are strings when
           present and not null (#713; a null is absent -- 0.9.x
           auto-save wrote one, review of #728). Nothing reads them; they are checked so no key
           the loader accepts carries an unchecked type.
        4. `redaction_zones` is a list, and only then are its zones
           walked: walked first, `redaction_zones: 5` escaped as a
           `TypeError`.
        5. Per zone mapping: unknown keys and `note`'s type, then the ROI
           checks, so a misspelt `rio:` is named rather than reported as
           a bad ROI.
        """
        sn = rule.get("serial_number")
        label = f"Rule #{index} ({sn})" if isinstance(sn, str) and sn.strip() else f"Rule #{index}"

        reason = _unknown_keys(rule, _RULE_KEYS, "", "A machine rule's")
        if reason is not None:
            raise ValueError(f"{label}: {reason}")

        if sn is None or sn == "":
            raise ValueError(f"Rule #{index}: Missing 'serial_number'.")
        if not isinstance(sn, str):
            raise ValueError(
                f"Rule #{index}: 'serial_number' must be a string, got {sn!r} "
                f"({type(sn).__name__}); quote it as it is written on the "
                f"machine (serial_number: \"0123\", not serial_number: 0123), "
                f"because YAML reads unquoted digits as a number, and a "
                f"leading 0 as octal: 0123 loads as 83 (#713)")
        if not sn.strip():
            # As an empty serial is (#730): no Device Serial Number is
            # blank, so the rule matched no machine and redacted nothing.
            raise ValueError(
                f"Rule #{index}: 'serial_number' {sn!r} is blank; give the "
                f"serial as it is written on the machine (#730)")

        for key in ("manufacturer", "model_name", "comment"):
            # Null is absent (the comment above `_VERSION_SHAPE`).
            if rule.get(key) is not None and not isinstance(rule[key], str):
                raise ValueError(
                    f"{label}: '{key}' must be a string, got {rule[key]!r} "
                    f"({type(rule[key]).__name__}) (#713)")

        zones = rule.get("redaction_zones", [])
        if not isinstance(zones, list):
            raise ValueError(f"Rule #{index} ({sn}): 'redaction_zones' must be a list.")

        for z_idx, zone in enumerate(zones):
            if isinstance(zone, list):
                roi = zone
            elif isinstance(zone, dict):
                reason = _unknown_keys(zone, _ZONE_KEYS, "", "A zone's")
                if reason is not None:
                    raise ValueError(f"{label}, Zone #{z_idx}: {reason}")
                # Null is absent (the comment above `_VERSION_SHAPE`).
                if zone.get("note") is not None and not isinstance(zone["note"], str):
                    raise ValueError(
                        f"{label}, Zone #{z_idx}: 'note' must be a string, got "
                        f"{zone['note']!r} ({type(zone['note']).__name__}) (#713)")
                roi = zone.get("roi")
            else:
                raise ValueError(
                    f"Rule #{index} ({sn}), Zone #{z_idx}: Invalid zone format (must be list or dict).")

            # The integer check is part of the shape check: a zone of
            # strings reached the `x < 0` below and escaped as TypeError,
            # which `load_config` then swallowed (#456).
            if (not roi or not isinstance(roi, list) or len(roi) != 4
                    or not all(isinstance(x, int) and not isinstance(x, bool)
                               for x in roi)):
                raise ValueError(
                    f"Rule #{index} ({sn}), Zone #{z_idx}: ROI must be a list of 4 integers.")

            r1, r2, c1, c2 = roi
            if any(x < 0 for x in roi):
                raise ValueError(
                    f"Rule #{index} ({sn}), Zone #{z_idx}: ROI values must be non-negative.")

            if r1 > r2 or c1 > c2:
                raise ValueError(
                    f"Rule #{index} ({sn}), Zone #{z_idx}: Invalid ROI logic (Start > End).")

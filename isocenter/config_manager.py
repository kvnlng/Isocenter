"""
Configuration manager for handling Isocenter system settings.

This module provides functionality to load, validate, and manage configuration
files for the Isocenter application: the unified YAML configuration
(schema version 2, checked key by key and type by type), external
privacy profile files, and the built-in profiles.
"""

import os
import logging
import copy
import difflib
from typing import Dict, Any, List, Optional
import re
import yaml

from .profiles import FLOOR, FLOOR_POLICY, PRIVACY_PROFILES, PROFILE_ALIASES

#: The schema version both writers stamp (`IsocenterConfiguration.save()`
#: and `Session.create_config()`, which read it at call time rather than
#: carrying their own literal). It is the schema's number, not the
#: package's. It never bumps the major in 1.x. A 1.x bumps the minor for
#: either of two reasons, and adds a row to
#: `tests/test_config_schema_version.py::SCHEMA_BY_VERSION` for each:
#:
#: - it adds a key or a value;
#: - it applies an unchanged file differently, for example a value-less
#:   REPLACE writing its VR's dummy.
#:
#: A 2.x file still loads unchanged under every later 2.x. A file declaring
#: a newer minor than this one is refused (`_declared_version`, #784):
#: this library cannot apply it the way that minor does.
#:
#: **Why the second reason matters.** The v1 policy fingerprint
#: (`configuration._canonical_policy_v1`) hashes this value, read at call
#: time. The bump is therefore the only thing that makes statuses scanned
#: under an older minor read as another policy. `export()` then says so
#: and the report grades REVIEW_REQUIRED until `audit()` runs again.
#: Without the bump, the fingerprint equates two scans that behave
#: differently. `tests/test_config_behaviour_is_versioned.py` pins what
#: this version does to fixed input, so such a change goes red there until
#: it bumps (#782); its `SHIPPED_BEHAVIOUR` freezes a released version's.
#:
#: **The working test** is `docs/api/stability.md`'s promise. Bump
#: whenever the same file would be applied differently to the same input:
#: findings, values a rule writes, tags a rule reaches, pixel zones or
#: date jitter. A re-audit does not re-check pixel zones or jitter, but the
#: fingerprint still moves, because the version is in it. Every bump moves
#: every fingerprint, so it re-measures the (0012,0063) literals in
#: `tests/test_an_export_says_how_it_was_de_identified.py` and retakes
#: `fingerprint/output.json`.
#:
#: Before the v1.0.0 tag, a change to a pinned profile's own rules is not
#: a bump: 2.0 is still the schema 1.0 ships, and those rules are in the
#: fingerprint themselves, so the policies built on the profile move and
#: `none` does not. After the tag the profile is frozen
#: (`tests/test_profile_editions.py`), and the one change it allows,
#: correcting a row the published standard shows was transcribed wrongly,
#: is a Breaking entry that raises the minor, since it applies an
#: unchanged file differently.
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

# The schema. Every key a configuration may carry, by level; a key outside
# these is refused by name rather than ignored, because an ignored key
# silently changes behaviour: a misspelt `remove_private_tag: false` would
# load and remove the private tags the file asked to keep. Extending the
# schema is one edit here, plus the minor bump `CONFIG_VERSION`'s comment
# describes.
_TOP_LEVEL_KEYS = frozenset({"version", "privacy_profile", "phi_tags",
                             "date_jitter", "remove_private_tags", "machines",
                             "burned_in_text"})
#: `comment` is the shipped knowledge bases' own field
#: (`resources/redaction_rules.json`, `ctp_rules.json`) and what
#: `create_config` and `save()` write; `manufacturer` is what `add_rule`
#: writes. Both are metadata and nothing reads them, but refusing either
#: would refuse the library's own output.
#: `match` is the second selector (#1108): a rule has exactly one of it
#: and `serial_number`. The two labels stay labels: `add_rule` writes
#: `'Unknown'` for both, so reading them as selectors would stop every
#: such rule matching.
_RULE_KEYS = frozenset({"serial_number", "match", "manufacturer",
                        "model_name", "redaction_zones", "comment"})
#: What a `match:` selector compares, exactly, against a series'
#: equipment (#1108).
_MATCH_KEYS = frozenset({"manufacturer", "model_name"})
#: `note` is written as data by `create_config` for every machine the
#: knowledge base matches by serial, so it is a zone key.
_ZONE_KEYS = frozenset({"roi", "note"})
#: `replacement` is deliberately absent and deliberately not reported as
#: unknown: it is the one known-refused key, and `_refused_phi_rule`
#: refuses it with its rename advice.
_PHI_RULE_KEYS = frozenset({"name", "action", "value", "target"})
#: An external profile file contributes its `phi_tags` and nothing else,
#: so any other key is refused: a profile carrying `privacy_profile: basic`
#: beside one rule would otherwise load as that one rule.
_PROFILE_FILE_KEYS = frozenset({"version", "phi_tags"})
#: The `burned_in_text` policy's keys (#1108). Joined the 2.0 row before
#: 1.0.0 shipped it (owner ruling Q7, 2026-10-14), and `scope` and
#: `modalities` joined the same row before 1.0.0 too (#1121; Q9 on #782:
#: rows freeze at final tags, and the key shipped only in rc19). `scope`
#: is required, by owner ruling of 2026-10-15: rc19 read every image
#: holding pixels, which on MIDI-B was ~24,000 CT/MR/PET slices, and a
#: default that reads everything is a choice the author makes on purpose.
#: No `min_box_height`: word boxes of a 24 px name block measured 17-21
#: px, and noise boxes on anatomy 9-62 px, so a height floor misses text
#: and spares no noise.
#: `target` (#1121, PR B) joined the same row before 1.0.0 by the same
#: ruling: optional, default `any`, narrowed to kinds of text. `detector`
#: (#1121, PR C) joined it by the same ruling (Q-X1 (a)).
_BURNED_IN_TEXT_KEYS = frozenset({"action", "min_confidence", "scope",
                                  "modalities", "target", "detector"})
#: What the policy does with text OCR finds on an image, in the order the
#: refusal lists them. Compared exactly, never case-folded: no `action`
#: is folded anywhere in a configuration (#754).
BURNED_IN_TEXT_ACTIONS = ("report", "redact", "withhold")
#: Which images the policy reads and gates (#1121), in the order the
#: refusal lists them: `flagged` (Burned In Annotation YES, an image this
#: store redacted, or one whose record holds a box), `modalities` (those,
#: and every image of a listed Modality), `all` (every image holding
#: pixels, rc19's reading). Compared exactly, as `action` is.
BURNED_IN_TEXT_SCOPES = ("flagged", "modalities", "all")
#: The kinds of text a narrowed `target` names (#1121, owner ruling Q-B1
#: (a)), in the order the refusal lists them; `text_kinds.KINDS` is the
#: detectors' list, which also holds `orgs` (a `SCRUB` kind only, #1130),
#: and a test keeps this a subset of it. `target: any` (the default) is
#: every region, as before the key. Compared exactly.
BURNED_IN_TEXT_KINDS = ("names", "dates", "ids")
#: The kinds a `SCRUB` rule's `target` names (#1130), in the order the
#: refusal lists them. `text_scrub.SCRUB_KINDS` is the scrub's copy, and
#: a test keeps the two equal. There is no `any`: `target` is required
#: (owner ruling Q5 (b)). Compared exactly. `phones`, `addresses`,
#: `sites` and `initials` are new values of the unshipped 2.0 row
#: (#1146), as `orgs` was.
SCRUB_KINDS = ("names", "dates", "ids", "orgs", "phones", "addresses",
               "sites", "initials")
#: The VRs `SCRUB` applies to on a standard tag: text a description can
#: carry. A Person Name is a name whole, so `REPLACE` is its answer.
_SCRUB_VRS = frozenset({"LO", "LT", "SH", "ST", "UC", "UT"})
#: The maximum length in characters of the text VRs that have one (PS3.5
#: Table 6.2-1). UC and UT have none. A local table, as
#: `_CREATOR_MAX_CHARACTERS` is, not pydicom's `MAX_VALUE_LEN`: a value
#: exactly this long may have been cut there, so `SCRUB`'s `names` reads
#: its last word as a name's beginning (#1146).
_TEXT_MAX_CHARACTERS = {"LO": 64, "SH": 16, "ST": 1024, "LT": 10240}
#: What decides "is this text?" (#1121, PR C), in the order the refusal
#: lists them: `tesseract` (rc19's OCR, the default) or `db` (the
#: PP-OCRv3 DB text detector run by OpenCV, the optional `textdetect`
#: extra). Compared exactly, as `action` is. No `east`: its weights are
#: GPL-3.0 (owner ruling Q-C2).
BURNED_IN_TEXT_DETECTORS = ("tesseract", "db")
#: The detector when the file names none: rc19's.
BURNED_IN_TEXT_DEFAULT_DETECTOR = "tesseract"
#: A Modality as DICOM writes it: a Code String of upper-case letters,
#: digits, underscore and space, at most 16 characters (PS3.5 6.2).
_MODALITY_CS = re.compile(r"[A-Z0-9_ ]{1,16}")
#: The threshold when the file names none. Measured on 23 bundled
#: anatomy-only images: no box at 80 or above, ten at 60 and above; the
#: name blocks and most initials read at 86-96 (#1108, Q3).
BURNED_IN_TEXT_DEFAULT_CONFIDENCE = 80
#: The threshold when the file names none, by detector. Under `db` it is
#: the DB box score times 100 (owner ruling Q-C5 (a)), and 80 was set by
#: the spike (2026-10-15): the lowest best score on an answer-key box was
#: 0.964 and the non-text boxes on radiographs scored 0.57-0.88, so 80
#: removes 3 of those 5 with a 0.16 margin below the lowest hit; 90
#: removes all 5 with 0.064; the zoo's 50 removes none.
BURNED_IN_TEXT_DEFAULT_CONFIDENCE_BY_DETECTOR = {
    "tesseract": BURNED_IN_TEXT_DEFAULT_CONFIDENCE, "db": 80}

# Null is read as absent for the five optional metadata strings -- a rule's
# `manufacturer`, `model_name` and `comment`, a zone's `note`, a phi rule's
# `name` -- and only for them. `add_rule(serial, eq.manufacturer,
# eq.model_name)` on equipment without those tags stores None, and so does
# `update_rule(serial, {"comment": None})`; a saved file can carry the
# null, and refusing it would refuse a file the library itself wrote.
# Nothing reads any of the five, so an absent value and a null one mean
# the same thing. This is not the rule for
# `remove_private_tags`, whose null is refused because it read as the
# opposite of the default, nor for `serial_number`, whose null is missing.
# The three checks that apply it say "null is absent" and point here.

#: `MAJOR.MINOR`, each a non-negative integer with no leading zero. One
#: schema version has one spelling, as one profile has: `"2.00"` and
#: `"02.0"` are refused as spellings, not read as 2.0 or as a major this
#: library does not read.
_VERSION_SHAPE = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
#: The looser shape, kept only to tell a leading zero from a non-version.
_VERSION_DIGITS = re.compile(r"[0-9]+\.[0-9]+")


def _declared_version(data: Dict[Any, Any], source: str) -> str:
    """The schema version `data` declares.

    Absent means `_UNVERSIONED_MEANS`. Present, it must be a `str` of the
    form `MAJOR.MINOR`, with no leading zero, whose major is
    `_READABLE_MAJOR` and whose minor is no newer than `CONFIG_VERSION`'s.

    Args:
        data (dict): The file's root mapping.
        source (str): The file's path, for the refusal.

    Returns:
        str: The declared version, or `_UNVERSIONED_MEANS`.

    Raises:
        ValueError: For a version that is not a string (an unquoted number
            or `null`), not `MAJOR.MINOR`, has a leading zero, names a
            major this library does not read (`"1.0"` included), or a
            minor newer than `CONFIG_VERSION`'s (#784).
    """
    # An unquoted number is refused rather than coerced -- YAML reads
    # `version: 2.10` as the float 2.1 -- and so is `null`: only an absent
    # line means "unversioned", and a bare `version:` is a typo.
    if "version" not in data:
        return _UNVERSIONED_MEANS
    version = data["version"]
    if not isinstance(version, str):
        raise ValueError(
            f"{source}: version must be a quoted string such as '2.0', got "
            f"{version!r} ({type(version).__name__}); unquoted, YAML reads "
            f"a version as a number, and 2.10 as the number 2.1")
    if _VERSION_DIGITS.fullmatch(version) and not _VERSION_SHAPE.fullmatch(version):
        raise ValueError(
            f"{source}: version {version!r} is not canonical: write "
            f"'MAJOR.MINOR' with no leading zero, such as '2.0'")
    if not _VERSION_SHAPE.fullmatch(version):
        raise ValueError(
            f"{source}: version {version!r} is not a 'MAJOR.MINOR' string "
            f"such as '2.0'")
    # `CONFIG_VERSION` is read at call time, not import time, so the
    # constant has one home.
    ours = CONFIG_VERSION
    if version.split(".")[0] != _READABLE_MAJOR:
        raise ValueError(
            f"{source}: version {version!r} is a configuration schema this "
            f"isocenter does not read; it reads version {_READABLE_MAJOR} "
            f"({_READABLE_MAJOR}.0 through {ours})")
    # Refused, not loaded with a note (owner ruling Q1 A on #784): a minor
    # bump means the same file is applied differently (#762), so a newer
    # file loaded here would be applied this library's way under its own
    # number, and `save()` would then rewrite it as ours. Minors compare as
    # integers, so "2.10" is newer than "2.9". Decided in 1.0.0: had 1.0
    # loaded a newer minor, no 1.x could refuse one (stability.md's "a file
    # 1.0 loads, every 1.x loads").
    if int(version.split(".")[1]) > int(ours.split(".")[1]):
        raise ValueError(
            f"{source}: version {version!r} is newer than this isocenter's "
            f"configuration version {ours}, which may apply it differently "
            f"than it was written for; upgrade isocenter, or set version: "
            f"{ours!r} to apply it as {ours} does")
    return version


def _refuse_overwriting_a_newer_minor(path: str) -> None:
    """Refuse to save over a file declaring a newer minor (#784).

    The load refuses such a file, but `config_path` can be assigned, and a
    save would then rewrite another isocenter's file as this version,
    erasing what it declared (owner ruling on #895). Only a readable
    mapping whose `version` is a canonical `MAJOR.MINOR` string of this
    major with a newer minor is refused: a missing, unreadable or
    malformed target is not this guard's to judge and is written as
    before, since the write replaces it whole.

    Args:
        path (str): The file `save()` is about to write.

    Raises:
        ValueError: When the file declares a newer minor. Nothing is
            written.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return
    if not isinstance(data, dict):
        return
    version = data.get("version")
    if not isinstance(version, str) or not _VERSION_SHAPE.fullmatch(version):
        return
    ours = CONFIG_VERSION
    major, minor = version.split(".")
    if major == _READABLE_MAJOR and int(minor) > int(ours.split(".")[1]):
        raise ValueError(
            f"{path}: declares version {version!r}, newer than this "
            f"isocenter's configuration version {ours}; saving would "
            f"rewrite it as {ours}. Nothing was written: upgrade isocenter, "
            f"or save to another path")


def _unknown_keys(keys, known, where: str, whose: str,
                  refused_elsewhere=frozenset()) -> Optional[str]:
    """The refusal for `keys` outside `known`, or None.

    Names **every** unknown key (sorted by `str`, since YAML allows
    `2:` and `yes:` as keys), offers `difflib`'s closest known key where
    it has one, and always lists the known keys.

    Args:
        keys: The keys present.
        known (frozenset): The keys the level accepts.
        where (str): Follows the key list: " at the top level", or "".
        whose (str): Names the level's keys, e.g. "A machine rule's".
        refused_elsewhere (frozenset): Keys a later check refuses with
            better words; only the phi-rule caller passes `replacement`.

    Returns:
        Optional[str]: The refusal text, or None when every key is known.
    """
    # `refused_elsewhere` is passed by the phi-rule caller alone, so no
    # other level exempts `replacement`.
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
    listed = f"{whose} keys are {', '.join(sorted(known))}"
    if len(unknown) == 1 and guesses:
        return f"{message}; did you mean {guesses[0][1]!r}? {listed}"
    if guesses:
        meant = ", ".join(f"{guess!r} for {key!r}" for key, guess in guesses)
        return f"{message}; did you mean {meant}? {listed}"
    return f"{message}; {listed}"


def _checked_top_level(data: Dict[Any, Any], source: str) -> str:
    """Check a configuration's version, then its top-level keys.

    Args:
        data (dict): The configuration's root mapping.
        source (str): The file's path, for the refusal.

    Returns:
        str: The declared version.

    Raises:
        ValueError: For a version `_declared_version` refuses, a
            `machine_rules` key (named as an old spelling of `machines`),
            or any unknown top-level key.
    """
    # The version first: a file written for another major may carry keys
    # this library has never heard of, and the version is the true reason
    # to refuse it.
    declared = _declared_version(data, source)
    if "machine_rules" in data:
        raise ValueError(
            f"{source}: 'machine_rules' is an old spelling of 'machines'; "
            f"rename it")
    reason = _unknown_keys(data, _TOP_LEVEL_KEYS, " at the top level",
                           "A version 2 configuration's")
    if reason is not None:
        raise ValueError(f"{source}: {reason}")
    return declared

#: Where this package's own shipped resources live.
#:
#: Nothing in this module reads a resource through it, and it stays:
#: `publish.yml`'s wheel gate passes it to `require_package_resource` from
#: an installed wheel, where it is the only spelling of "this package's
#: resources directory" that does not point back at the source tree.
RESOURCES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "resources")

# There is deliberately no `load_dotenv()` at import: it would search
# upward from this file's directory, so whether a project's `.env` applied
# would depend on where the virtual environment lives, and importing a
# library would change the process environment. A caller who wants a
# `.env` loads it before importing.
# `tests/test_import_does_not_read_a_dotenv.py` holds this.


def require_package_resource(directory: str, basename: str,
                             consequence: str) -> str:
    """The path to a resource this package ships, or a refusal.

    A missing shipped resource is a broken install, so this refuses rather
    than letting the caller continue with an empty collection; no audit
    row is written. Call it **before** any `try` that catches `OSError`.

    Args:
        directory (str): the resources directory to look in.
        basename (str): the file's name, passed as a bare literal by every
            caller so `test_every_shipped_resource_is_named_by_the_package`
            can still see it in the AST.
        consequence (str): the caller's own words for what a silent
            continue would have done, e.g. "scanned every frame with no
            machine redaction rules"; it is quoted in the refusal.

    Returns:
        str: the resolved path, which exists.

    Raises:
        RuntimeError: if the resource is not there.
    """
    # A refusal, not a warning: continuing would scan every frame with no
    # redaction rules, or audit against an empty PHI tag list, and report
    # clean. Unlike the optional extras, which degrade gracefully,
    # `setup.py`'s `package_data` promises the resource and `publish.yml`
    # refuses to release a wheel without it.
    #
    # `RuntimeError`, deliberately not `FileNotFoundError`:
    # `ConfigLoader._load_yaml` raises that for a *user's* config file, a
    # different failure with a different remedy; and the callers' handlers
    # are `except (OSError, ...)`, which would catch a `FileNotFoundError`
    # and turn it straight back into the empty collection.
    #
    # `directory` is a parameter rather than a module global read here,
    # because tests monkeypatch the callers' `RESOURCES_DIR`; a helper that
    # closed over its own copy would make every such test pass against the
    # real source tree.
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
    """`tags` with every string key in its one spelling
    (`_canonical_rule_key`: lowercased, a private creator's case kept),
    order kept.

    Args:
        tags (dict): The mapping.

    Returns:
        dict: A new mapping; non-string keys are kept as they are.
    """
    return {(_canonical_rule_key(key) if isinstance(key, str) else key): value
            for key, value in tags.items()}


def _names_no_profile(profile_name: Any) -> bool:
    """True for `privacy_profile: none`, in any case and with surrounding
    whitespace.

    Not for `privacy_profile: null` (a bare `privacy_profile:` line), which
    is absent and means the floor, as leaving the line out does.

    Args:
        profile_name: The `privacy_profile` value.

    Returns:
        bool: Whether it is the string `none`.
    """
    return isinstance(profile_name, str) and profile_name.strip().lower() == "none"


#: The actions `PhiInspector._scan_instance` dispatches on (`REPLACE` is
#: its `else` arm). Anything else is refused at load, because the scan
#: would otherwise treat `action: OBLITERATE` as REPLACE without a word.
_PHI_ACTIONS = frozenset({"KEEP", "REMOVE", "EMPTY", "REPLACE", "SHIFT", "JITTER",
                          "SCRUB"})


_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _is_tag_key(key: str) -> bool:
    """True for a `gggg,eeee` key: four hex digits, a comma, four more.

    The spelling every tag table here uses, and the only one the scan
    looks up: a rule keyed `'8,80'` (for 0008,0080) or by a keyword such
    as `PatientName` would match nothing and never run.

    Args:
        key (str): The `phi_tags` key.

    Returns:
        bool: Whether it has that shape.
    """
    return (len(key) == 9 and key[4] == ","
            and all(ch in _HEX_DIGITS for ch in key[:4] + key[5:]))


#: PS3.15 Table E.1-1's own spellings of a repeating group:
#: `50xx,xxxx` / `60xx,xxxx` for a whole group, `50xx,eeee` / `60xx,eeee`
#: for one element in every group. Matched on the lowercased key, so
#: either case of `x` is read.
_GROUP_MASK_KEY = re.compile(r"(50|60)xx,(xxxx|[0-9a-f]{4})")


def _is_group_mask_key(key: str) -> bool:
    """True for a repeating-group key (`50xx,xxxx`, `60xx,eeee`, ...).

    Such a key names the **even** groups 5000-501E (retired Curve) or
    6000-601E (Overlay) and nothing else (PS3.5 7.6); the odd groups
    between them are private and belong to the `remove_private_tags`
    sweep. `privacy._rule_for` resolves a mask to the concrete tags it
    covers, the most specific key first.

    Args:
        key: The `phi_tags` key; matched lowercased.

    Returns:
        bool: Whether it is a repeating-group mask.
    """
    return isinstance(key, str) and _GROUP_MASK_KEY.fullmatch(key.lower()) is not None


#: A private-creator key (#1132): `gggg,"CREATOR",ee`, an odd group, the
#: creator's value in double quotes, and the element's offset in the
#: creator's block. PS3.15 Table E.3.10-1 names a private element by the
#: same two halves, and so does the MIDI-B answer key.
_CREATOR_KEY = re.compile(r'([0-9a-fA-F]{4}),"(.*)",([0-9a-fA-F]{2})', re.S)
#: Groups PS3.5 7.8.1 says are not private although they are odd.
_NOT_PRIVATE_GROUPS = frozenset({0x0001, 0x0003, 0x0005, 0x0007, 0xFFFF})
#: An LO holds 64 characters (PS3.5 6.2), and a creator is an LO.
_CREATOR_MAX_CHARACTERS = 64
#: The key shapes a `phi_tags` key may take. A new shape is a new value a
#: 2.0 file could hold, so it comes with a schema minor once 1.0.0 is
#: tagged (`tests/test_config_schema_version.py` reads this).
PHI_KEY_SHAPES = ("tag", "mask", "creator")


def _creator_key_refused(key: Any) -> Optional[str]:
    """Why `key`, which holds a double quote, is not a private-creator key,
    or None when it is one (or holds no quote, so is not trying to be).

    Args:
        key: The `phi_tags` key.

    Returns:
        Optional[str]: The refusal, naming the key, or None.
    """
    if not isinstance(key, str) or '"' not in key:
        return None
    match = _CREATOR_KEY.fullmatch(key)
    why = None
    if match is None:
        why = ("it is not four hex digits, a comma, the creator in double "
               "quotes, a comma and two hex digits")
    else:
        group = int(match.group(1), 16)
        creator = match.group(2).strip(" ")
        if group % 2 == 0 or group in _NOT_PRIVATE_GROUPS:
            why = (f"group {match.group(1)} is not private (PS3.5 7.8.1: an "
                   f"odd group other than 0001, 0003, 0005, 0007 and ffff)")
        elif not creator:
            why = "the creator is empty"
        elif len(creator) > _CREATOR_MAX_CHARACTERS:
            why = (f"the creator is {len(creator)} characters, and a private "
                   f"creator is an LO of at most {_CREATOR_MAX_CHARACTERS}")
        elif any(ch == "\\" or ord(ch) < 0x20 or ord(ch) == 0x7F
                 for ch in creator):
            why = ("the creator holds a backslash or a control character, "
                   "which an LO cannot")
    if why is None:
        return None
    return (f"phi_tags key {key!r} is not a private-creator key: {why}. The "
            f"shape is 'gggg,\"CREATOR\",ee', such as "
            f"'0009,\"GEMS_IDEN_01\",27'")


def _is_creator_key(key: Any) -> bool:
    """True for a well-formed private-creator key (`_creator_key_refused`
    finds nothing wrong with it).

    Args:
        key: The `phi_tags` key.

    Returns:
        bool: Whether it is one.
    """
    return (isinstance(key, str) and '"' in key
            and _creator_key_refused(key) is None)


def _creator_key_parts(key: str):
    """`(group, creator, offset)` of a private-creator key: the group and
    offset as ints, the creator stripped of its LO padding.

    Args:
        key (str): A key `_is_creator_key` accepts.

    Returns:
        tuple: `(int, str, int)`.
    """
    match = _CREATOR_KEY.fullmatch(key)
    return (int(match.group(1), 16), match.group(2).strip(" "),
            int(match.group(3), 16))


#: ASCII `a`-`z` to `A`-`Z` and nothing else, so no other letter is
#: touched (`'ß'` stays `'ß'`, never `'SS'`): `pixel_geometry._cs_spelling`'s
#: rule.
_ASCII_UPPER = str.maketrans("abcdefghijklmnopqrstuvwxyz",
                             "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_SPACE_RUN = re.compile(" +")


def _creator_identity(creator: str) -> str:
    """The one identity a private creator is matched by (#1147).

    Surrounding spaces stripped, each run of interior spaces made one, and
    ASCII `a`-`z` upper-cased. PS3.5 7.8.1 says a creator is an LO
    "identification code" and has no rule for comparing two of them;
    vendors write one creator in more than one case (pydicom's own private
    dictionary holds `HOLOGIC` and `Hologic` with the same elements, and
    the collection of issue 1147 both `...011710XX` and `...011710xx`).
    Only the space is folded: an LO holds no control character but ESC,
    and a key with a tab is refused. A creator that differs in anything else
    (`GEMS_IDEN_1` against `GEMS_IDEN_01`, a non-ASCII letter's case) is
    another creator.

    Matching folds; a key's spelling never does (`_canonical_rule_key`).

    Args:
        creator (str): A creator, from a key or from a file.

    Returns:
        str: Its identity.
    """
    return _SPACE_RUN.sub(" ", creator.strip(" ")).translate(_ASCII_UPPER)


def _canonical_rule_key(key: str) -> str:
    """The one spelling of a `phi_tags` key.

    A tag or mask is lowercased. A private-creator key has its group and
    offset lowercased and its creator kept as written, stripped of
    surrounding spaces. The key keeps the creator's case and spacing
    because the policy form hashes keys as held (`configuration.
    _canonical_policy_v1`): folding here would move every creator-keyed
    store's fingerprint. The scan folds instead, at matching
    (`_creator_identity`, #1147). Every site that spells a key goes
    through here (#1132).

    Args:
        key (str): The key as written.

    Returns:
        str: Its spelling in the policy.
    """
    if _is_creator_key(key):
        group, creator, offset = _creator_key_parts(key)
        return f'{group:04x},"{creator}",{offset:02x}'
    return key.lower()


def _external_profile_tags(path: str) -> Dict[str, Any]:
    """The validated `phi_tags:` mapping of an external profile file.

    The file's own `version` is checked as a configuration's is, and it
    may carry only `phi_tags` and `version`.

    Args:
        path (str): The profile file.

    Returns:
        dict: Its `phi_tags`, validated and lowercase-keyed
        (`_validated_phi_tags`).

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: Naming the file, for invalid YAML, a root that is not
            a mapping, no `phi_tags` key, a refused version, any other
            key, or `phi_tags` that `_validated_phi_tags` refuses.
    """
    # The root mapping is never read as the tags, so a profile written as
    # a config is not loaded with `privacy_profile` as a "tag". The version
    # first, as in a configuration; the no-`phi_tags` refusal before the
    # key check, so a root mapping of tags keeps its own message rather
    # than being told every tag is an unknown key.
    data = ConfigLoader._load_yaml(path)
    if not isinstance(data, dict):
        raise ValueError(
            f"{path}: an external privacy profile must carry its rules under "
            f"a 'phi_tags:' mapping; this file has no phi_tags key")
    _declared_version(data, path)
    if "phi_tags" not in data:
        raise ValueError(
            f"{path}: an external privacy profile must carry its rules "
            f"under a 'phi_tags:' mapping; this file has no phi_tags key")
    unknown = sorted((k for k in data if k not in _PROFILE_FILE_KEYS), key=str)
    if unknown:
        raise ValueError(
            f"{path}: an external privacy profile contributes only its "
            f"phi_tags; unknown key(s) {', '.join(repr(k) for k in unknown)} "
            f"would be ignored")
    return _validated_phi_tags(data["phi_tags"], path)


def _refused_date_jitter(dj: Any) -> Optional[str]:
    """Why `dj` is not a `date_jitter` this library applies, or None.

    The one judge of a range, for a file's (the loader) and for one
    assigned in code (`audit()`, `anonymize()`, `save()`,
    `create_config()` and `RemediationService`, #731), so the two cannot
    disagree or be worded twice. The caller prefixes its source.

    Args:
        dj: The range: None (the default), or `{min_days: int, max_days:
            int}` with `min_days <= max_days`.

    Returns:
        Optional[str]: The refusal, or None.
    """
    # One shape, {min_days: int, max_days: int}; a bare int is refused
    # with the mapping to write instead, and a bool is not an int here.
    # None is the default, as a null `date_jitter:` is in a file.
    if dj is None:
        return None
    if isinstance(dj, int) and not isinstance(dj, bool):
        return (f"'date_jitter' must be {{min_days: int, max_days: int}}; the "
                f"single-int form was removed in 1.0 -- write {{min_days: "
                f"{dj}, max_days: {dj}}} for the same fixed shift")
    if not (isinstance(dj, dict) and set(dj) == {"min_days", "max_days"}
            and all(isinstance(v, int) and not isinstance(v, bool)
                    for v in dj.values())):
        return (f"'date_jitter' must be {{min_days: int, max_days: int}}, "
                f"got {dj!r}")
    # Bounds the wrong way round have at least one of them wrong, and
    # nothing can tell which. Equal bounds are a fixed shift.
    if dj["min_days"] > dj["max_days"]:
        return (f"'date_jitter' min_days {dj['min_days']} is greater than "
                f"max_days {dj['max_days']}; one of them is wrong, and which "
                f"cannot be told from the range alone")
    return None


def _refuse_date_jitter_in_code(dj: Any) -> None:
    """Raise `_refused_date_jitter`'s refusal for a range assigned in code.

    Args:
        dj: `session.configuration.date_jitter`.

    Raises:
        ValueError: When the loader would refuse the same range in a file.
    """
    reason = _refused_date_jitter(dj)
    if reason is not None:
        raise ValueError(f"session.configuration: {reason}")


def _refused_burned_in_text(policy: Any) -> Optional[str]:
    """Why `policy` is not a `burned_in_text` this library applies, or None.

    The one judge, for a file's (the loader) and for one assigned in code
    (`redact()`, `export()`, `save()`, `create_config()`), so the two
    cannot disagree or be worded twice. The caller prefixes its source.

    Judged in one order, so a file gets one sentence: the mapping, its
    keys, `action`, `scope`, `modalities`, `target`, `detector`,
    `min_confidence`.

    Args:
        policy: None (no policy), or a mapping with a required `action`
            (one of `BURNED_IN_TEXT_ACTIONS`), a required `scope` (one of
            `BURNED_IN_TEXT_SCOPES`), a `modalities` list required under
            `scope: modalities` and refused under any other, an optional
            `target` (`any` or a list of `BURNED_IN_TEXT_KINDS`), an
            optional `detector` (one of `BURNED_IN_TEXT_DETECTORS`), and an
            optional `min_confidence` (a number from 0 to 100, not a bool).

    Returns:
        Optional[str]: The refusal, or None.
    """
    if policy is None:
        return None
    if not isinstance(policy, dict):
        return (f"'burned_in_text' must be a mapping such as "
                f"{{action: redact, scope: flagged}}, got {policy!r}")
    reason = _unknown_keys(policy, _BURNED_IN_TEXT_KEYS, "", "burned_in_text's")
    if reason is not None:
        return f"'burned_in_text': {reason}"
    action = policy.get("action")
    if action is None:
        return ("'burned_in_text' needs an 'action': one of "
                f"{', '.join(BURNED_IN_TEXT_ACTIONS)}")
    if action not in BURNED_IN_TEXT_ACTIONS:
        return (f"'burned_in_text' action {action!r} is not one of "
                f"{', '.join(BURNED_IN_TEXT_ACTIONS)} (exact spelling)")
    reason = _refused_burned_in_text_scope(policy)
    if reason is not None:
        return reason
    reason = _refused_burned_in_text_target(policy)
    if reason is not None:
        return reason
    if ("detector" in policy
            and (not isinstance(policy["detector"], str)
                 or policy["detector"] not in BURNED_IN_TEXT_DETECTORS)):
        # A `null` is refused as a value, as `scope`'s is.
        return (f"'burned_in_text' detector {policy['detector']!r} is not one "
                f"of {', '.join(BURNED_IN_TEXT_DETECTORS)} (exact spelling)")
    if "min_confidence" in policy:
        conf = policy["min_confidence"]
        # A bool is not a number here, and a quoted "80" is text. NaN and
        # the infinities fail the range test, which is written so they do.
        if isinstance(conf, bool) or not isinstance(conf, (int, float)):
            return (f"'burned_in_text' min_confidence must be a number from "
                    f"0 to 100, got {conf!r} ({type(conf).__name__})")
        if not 0 <= conf <= 100:
            return (f"'burned_in_text' min_confidence must be a number from "
                    f"0 to 100, got {conf!r}")
    return None


def _refused_burned_in_text_scope(policy: Dict[Any, Any]) -> Optional[str]:
    """Why a `burned_in_text` mapping's `scope` and `modalities` are not
    ones this library applies, or None (#1121).

    A `scope` holding `null` is refused as a value, never read as absent:
    only the whole `burned_in_text` key's `null` is absent. A Modality in
    lower case is refused, not folded: on the wire it is an upper-case
    Code String, and a fold here would be the only case fold in a
    configuration (#754's rule for `action`).

    Args:
        policy (dict): A mapping whose keys and `action` are already judged.

    Returns:
        Optional[str]: The refusal, or None.
    """
    scopes = ", ".join(BURNED_IN_TEXT_SCOPES)
    if "scope" not in policy:
        return f"'burned_in_text' needs a 'scope': one of {scopes}"
    scope = policy["scope"]
    if not isinstance(scope, str) or scope not in BURNED_IN_TEXT_SCOPES:
        return (f"'burned_in_text' scope {scope!r} is not one of {scopes} "
                f"(exact spelling)")
    if scope != "modalities":
        # No silent ignore, as for every other key in the file: a list
        # under `all` would read as a narrowing that is not applied.
        if "modalities" in policy:
            return (f"'burned_in_text' modalities applies only under scope: "
                    f"modalities; scope is {scope!r}")
        return None
    if "modalities" not in policy:
        return ("'burned_in_text' scope modalities needs 'modalities': a list "
                "of Modality values such as [CR, DX, MG]")
    values = policy["modalities"]
    if not isinstance(values, list):
        return (f"'burned_in_text' modalities must be a list such as "
                f"[CR, DX, MG], got {values!r} ({type(values).__name__})")
    if not values:
        return ("'burned_in_text' modalities is empty; name at least one "
                "Modality, or use scope: flagged")
    for item in values:
        if (not isinstance(item, str) or not item.strip()
                or not _MODALITY_CS.fullmatch(item)):
            return (f"'burned_in_text' modalities item {item!r} is not a "
                    f"Modality as DICOM writes it (upper case: CR, DX, MG)")
    return None


def _refused_burned_in_text_target(policy: Dict[Any, Any]) -> Optional[str]:
    """Why a `burned_in_text` mapping's `target` is not one this library
    applies, or None (#1121). Absent is `any`; `null` is refused as a
    value, as `scope`'s is. One spelling for one kind: `[names]`, never
    `names`.

    Args:
        policy (dict): A mapping whose earlier keys are already judged.

    Returns:
        Optional[str]: The refusal, or None.
    """
    if "target" not in policy:
        return None
    target = policy["target"]
    kinds = ", ".join(BURNED_IN_TEXT_KINDS)
    if not isinstance(target, list):
        if isinstance(target, str) and target == "any":
            return None
        return (f"'burned_in_text' target {target!r} must be any or a list "
                f"of kinds such as [{kinds}]")
    if not target:
        return ("'burned_in_text' target is an empty list; name at least one "
                f"of {kinds}, or write any")
    if "any" in target and len(target) > 1:
        return ("'burned_in_text' target lists any beside other kinds; write "
                "target: any alone")
    for item in target:
        if not isinstance(item, str) or item not in BURNED_IN_TEXT_KINDS:
            return (f"'burned_in_text' target item {item!r} is not one of "
                    f"{kinds} (exact spelling)")
    return None


def _burned_in_text_policy(policy: Any, source: str) -> Optional[Dict[str, Any]]:
    """The policy with its default filled in, or None; refused when the
    loader would refuse it.

    Filled in so `{action: redact, scope: all}` and `{action: redact,
    scope: all, min_confidence: 80}` are one policy wherever it is
    compared or hashed; `modalities` is held as its sorted distinct
    values, each without a Code String's padding, so a repeat or an order
    changes nothing a reader sees.

    Args:
        policy: What the file or the attribute holds.
        source (str): Prefixes the refusal: the file's path, or
            `session.configuration`.

    Returns:
        Optional[Dict[str, Any]]: `{action, scope, detector,
            min_confidence, target}` (the threshold's default is the
            detector's), plus
            `modalities` under `scope: modalities`; a new dict, or None.

    Raises:
        ValueError: `_refused_burned_in_text`'s refusal.
    """
    reason = _refused_burned_in_text(policy)
    if reason is not None:
        raise ValueError(f"{source}: {reason}")
    if policy is None:
        return None
    detector = policy.get("detector", BURNED_IN_TEXT_DEFAULT_DETECTOR)
    normalised = {"action": policy["action"], "scope": policy["scope"],
                  "detector": detector,
                  "min_confidence": policy.get(
                      "min_confidence",
                      BURNED_IN_TEXT_DEFAULT_CONFIDENCE_BY_DETECTOR[detector])}
    if policy["scope"] == "modalities":
        normalised["modalities"] = sorted({item.strip()
                                           for item in policy["modalities"]})
    # `any`, or the sorted distinct kinds: a repeat or an order changes
    # nothing a reader, a hash or a saved file sees.
    target = policy.get("target", "any")
    normalised["target"] = target if target == "any" else sorted(set(target))
    return normalised


def _phi_rule_shape_refused(tag: Any, rule: Dict[Any, Any]) -> Optional[str]:
    """Why a rule mapping's keys or `name` are not the schema's, or None.

    A key outside `name`, `action` and `value` is refused (`replacement`
    excepted: `_refused_phi_rule` refuses it with its rename advice), and
    `name` must be a string when present; null is absent, as for the
    other optional metadata strings (see the comment above
    `_VERSION_SHAPE`). Callers ask this first, ahead of the action and VR
    checks.

    Args:
        tag (str): The rule's key, for the message.
        rule (dict): The rule mapping.

    Returns:
        Optional[str]: The refusal, or None.
    """
    # First on every door: an ignored misspelt key such as `{actoin: KEEP}`
    # would leave the action at REPLACE, replacing the value the file asked
    # to keep, or be refused for a value it never asked to write.
    reason = _unknown_keys(rule, _PHI_RULE_KEYS, "", "A rule's",
                           refused_elsewhere=frozenset({"replacement"}))
    if reason is not None:
        return f"phi_tags[{tag!r}] has {reason}"
    if rule.get("name") is not None and not isinstance(rule["name"], str):
        return (f"phi_tags[{tag!r}] name must be a string, got "
                f"{type(rule['name']).__name__}")
    if "target" in rule:
        return _scrub_target_refused(tag, rule["target"])
    return None


def _scrub_target_refused(tag: Any, target: Any) -> Optional[str]:
    """Why a rule's `target` is not a list of distinct `SCRUB_KINDS`, or
    None (#1130). Whether the action takes one is `_refused_phi_rule`'s.

    Args:
        tag (str): The rule's key, for the message.
        target: The `target` value.

    Returns:
        Optional[str]: The refusal, or None.
    """
    kinds = ", ".join(SCRUB_KINDS)
    if not isinstance(target, list):
        # A string is refused rather than read as one kind: `set_phi_tag`
        # would otherwise iterate it into letters.
        return (f"phi_tags[{tag!r}] target must be a list of kinds, such as "
                f"['dates'], got {target!r}")
    if not target:
        return (f"phi_tags[{tag!r}] target is an empty list; name at least "
                f"one of {kinds}")
    for item in target:
        if not isinstance(item, str) or item not in SCRUB_KINDS:
            return (f"phi_tags[{tag!r}] target item {item!r} is not one of "
                    f"{kinds} (exact spelling)")
    for item in target:
        if target.count(item) > 1:
            return f"phi_tags[{tag!r}] target lists {item!r} twice"
    return None


def _validated_phi_tags(tags: Any, source: str) -> Dict[str, Any]:
    """`tags` as a lowercase-keyed mapping, or a `ValueError` naming the tag.

    A tag's key is a `gggg,eeee` string or a repeating-group key, and its
    value is a display name (a string, which leaves the action at
    REPLACE) or a rule mapping whose keys are the schema's and whose
    `action`, if present, is one the inspector implements.

    Args:
        tags: The `phi_tags` value; None (a bare `phi_tags:` line) is the
            empty mapping.
        source (str): The file's path, for the refusal.

    Returns:
        dict: The mapping with lowercased keys.

    Raises:
        ValueError: For `tags` that is not a mapping, a key that is not a
            quoted tag or mask, a rule `_phi_rule_shape_refused` refuses,
            an unknown action, or a value that is neither a string nor a
            mapping.
    """
    # Every other shape is refused here rather than failing or misbehaving
    # later in the scan.
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
        creator = _creator_key_refused(tag)
        if creator is not None:
            raise ValueError(f"{source}: {creator}")
        if not (_is_tag_key(tag) or _is_group_mask_key(tag)
                or _is_creator_key(tag)):
            raise ValueError(
                f"{source}: phi_tags key {tag!r} is not a 'gggg,eeee' tag "
                f"(four hex digits, a comma, four hex digits, such as "
                f"'0010,0010'), or a repeating-group key such as "
                f"'60xx,xxxx', or a private-creator key such as "
                f"'0009,\"GEMS_IDEN_01\",27'; the scan reads no tag by that "
                f"key, so the rule would never run")
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
#: tag's dictionary VR. PS3.15 Table E.1-1a's D is "replace with a
#: non-zero length value that may be a dummy value and consistent with the
#: VR"; `basic@2026c` maps D and every code with a D arm to that REPLACE.
#:
#: - Text: `ANONYMIZED`, the value REPLACE writes on text. Ten characters,
#:   under every limit (16 for AE, CS, SH), and valid in each of these VRs
#:   (a relative reference for UR).
#: - DA and DT `19000101` (a DT may stop at the date: no time of day is
#:   invented, see `exporters/wfdb.py::_real_timing`), TM `000000`, AS
#:   `000D`: visibly placeholder values, not plausible real ones.
#: - Binary: zero bytes, one word of the VR's width, so the value field is
#:   even (PS3.5 7.1.1).
#:
#: A VR not here has no dummy (`_vr_dummy` is None). A value-less REPLACE
#: on UI is the keyed UID replacement instead (`privacy.
#: _replacement_uid_for`), never a constant: one dummy UID would merge
#: every study under it. On the rest it is refused: numeric VRs and AT (no
#: Table E.1-1 D row is either, and a zero reads as a plausible value), SQ
#: (no dummy item is valid independent of the IOD; the scan warns and
#: applies nothing), and a compound dictionary VR.
#: Changing a value here changes `basic@2026c`'s output, which is frozen
#: for 1.x.
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
    """The dummy a value-less REPLACE writes on `tag` (`VR_DUMMY`).

    Chosen by the tag's dictionary VR, never the recorded one, so the
    value a rule writes does not depend on the file it meets. One value,
    whatever the tag's multiplicity.

    Args:
        tag (str): A `gggg,eeee` key.

    Returns:
        Any: The dummy (`str` or `bytes`), or None when the dictionary VR
        has none, and for a private, unknown or malformed tag (which keeps
        `ANONYMIZED` and the exporter's LO fallback).
    """
    # One value is enough: every D-arm row of Table E.1-1 is VM 1 or 1-n.
    return VR_DUMMY.get(_standard_dictionary_vr(tag))


#: The three tags the Patient and the Study own. Their rules are
#: read by `privacy._owned_rule`; the validator knows two things about
#: them: Patient ID cannot be emptied, removed, shifted or given a
#: literal, and Study Date's REPLACE with no value is the shift.
_PATIENT_ID = "0010,0020"
_STUDY_DATE = "0008,0020"

#: The two UIDs a Study and a Series own, with the name and the owner the
#: refusal says (#877). The exporter stamps every file's copy from its
#: owner (#624, #544), and REMOVE or EMPTY never changed the owner, so
#: the export wrote the source UID; under REMOVE it graded PASS and wrote
#: `(0012,0062) YES`. A mask key cannot reach either (`_GROUP_MASK_KEY` is
#: 50xx/60xx only), so the concrete key is the whole of it.
_OWNED_UIDS = {"0020,000d": ("Study Instance UID", "study"),
               "0020,000e": ("Series Instance UID", "series")}


def _standard_dictionary_vr(tag: str) -> Optional[str]:
    """The dictionary VR of a standard (even-group) tag.

    Args:
        tag (str): A `gggg,eeee` key.

    Returns:
        Optional[str]: The VR (a compound one such as `US or SS` as
        pydicom spells it), or None for a private tag, an unknown one,
        and a key that is not a tag.
    """
    # No parity test: pydicom's standard dictionary, repeaters included,
    # holds no odd-group entry, so a private tag is a `KeyError` like any
    # unknown one.
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


def _text_max_characters(tag: str, recorded_vr: Optional[str] = None) -> Optional[int]:
    """The maximum length in characters of `tag`'s text VR, or None (#1146).

    A standard tag's VR is its dictionary's; a tag the dictionary does
    not know (a private one) takes the VR the source recorded for it.
    Characters, not bytes: a value a site cut by bytes under a multi-byte
    character set is shorter in characters, and is not seen as cut.

    Args:
        tag (str): A `gggg,eeee` key.
        recorded_vr (str, optional): The item's recorded VR for `tag`
            (`DicomItem.attribute_vrs`).

    Returns:
        Optional[int]: From `_TEXT_MAX_CHARACTERS` (LO, SH, ST, LT); None
        for UC, UT, any other VR and an unknown one.
    """
    vr = _standard_dictionary_vr(tag) or recorded_vr
    return _TEXT_MAX_CHARACTERS.get(vr) if isinstance(vr, str) else None


def _dictionary_vm(tag: str) -> Optional[str]:
    """The dictionary value multiplicity of a standard tag.

    Args:
        tag (str): A `gggg,eeee` key.

    Returns:
        Optional[str]: The VM (`'1'`, `'1-n'`, ...), or None where
        `_standard_dictionary_vr` is None.
    """
    from pydicom.datadict import dictionary_VM  # pylint: disable=import-outside-toplevel
    if _standard_dictionary_vr(tag) is None:
        return None
    return str(dictionary_VM(int(tag.replace(",", ""), 16)))


def _vm_allows(vm: str, count: int) -> bool:
    """Whether `count` values meet a dictionary VM.

    Args:
        vm (str): pydicom's spelling: `'N'`, `'N-M'`, `'N-n'` or `'N-Nn'`
            (`'2-2n'`: 2, 4, 6, ...).
        count (int): How many values.

    Returns:
        bool: Whether the count is allowed; True for any spelling outside
        those four.
    """
    # An unparsed spelling allows any count, so a dictionary refresh cannot
    # refuse a rule by a VM this does not read.
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
    """True when a `-` in a DT string is not its UTC offset.

    The offset is `&ZZXX` at the end -- `+` or `-`, hours at most 14,
    minutes below 60 (PS3.5 Table 6.2-1) -- so one such suffix is set
    aside and any `-` left is a range: `20230101-20230201`, `-20230201`,
    `20230101-`, and `2023-2024`, whose 20 is no hour of an offset.
    `2023-1200` stays a year at offset -1200, as PS3.5 reads it.

    Args:
        value (str): The DT value.

    Returns:
        bool: Whether it is a range.
    """
    match = re.search(r"[+-]([0-9]{2})([0-9]{2})$", value)
    if match and int(match.group(1)) <= 14 and int(match.group(2)) < 60:
        value = value[:match.start()]
    return "-" in value


def _is_oversized_tag_key(tag: str) -> bool:
    """True for a key that reads as hex but names no 32-bit tag, such as
    `'10000,0010'`.

    Args:
        tag (str): The key.

    Returns:
        bool: Whether it is hex beyond 32 bits.
    """
    # Refused by the caller as a non-tag key: pydicom's `Tag` would raise
    # `OverflowError`, not the `ValueError` every other refusal is.
    try:
        number = int(tag.replace(",", ""), 16)
    except ValueError:
        return False
    return not 0 <= number <= 0xFFFFFFFF


def _dictionary_vr_refuses(tag: str, value: Any) -> Optional[str]:
    """The dictionary VR of a **standard** tag when that VR cannot hold
    `value`, else None.

    Judged by pydicom's `validate_value`, with AT always refused and a
    compound VR (`US or SS`) refused only when no arm holds the value. On
    a tag whose multiplicity allows several values, a string is judged
    one `\\`-separated value at a time; a list or tuple one element at a
    time.

    Args:
        tag (str): A `gggg,eeee` key.
        value: What the rule writes.

    Returns:
        Optional[str]: The VR that cannot hold it, or None -- also for a
        private (odd-group) tag, an unknown tag, and a sequence.
    """
    # None for a private tag because the exporter writes a private value
    # its recorded VR cannot hold under one that holds it, with a WARNING
    # row, so a refusal would keep an identifier the write removes; an
    # unknown tag has no VR to judge by; a value on a sequence is warned
    # about by the scan, not written.
    #
    # `validate_value`, not `io_handlers._value_fits_vr`, which skips
    # repertoire by design (a lower-case CS fits) and checks format only for
    # DA, DT, TM, UI and AS. AT is refused outright because pydicom does not
    # validate an AT string; a compound VR is split because pydicom has no
    # validator under that name and passes anything. A multi-valued string
    # is split because `validate_value` reads it as one value and would
    # refuse `A\P` on Patient Orientation (CS, VM 2) for the backslash. One
    # value on a VM-1 tag holding a backslash is `_refused_phi_rule`'s to
    # refuse.
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
        # replacement of a VM 1-n element is a list, and
        # `validate_value` reads a list as one value and refuses it.
        parts = list(value)
    elif isinstance(value, str) and _dictionary_vm(tag) != "1":
        parts = value.split("\\")

    def holds(arm):
        """Whether every part is valid under one VR arm.

        Args:
            arm (str): One VR of the dictionary VR.

        Returns:
            bool: False for AT, and for any part `validate_value` refuses.
        """
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
    """Why this one rule cannot be honoured, or None.

    Refused, in this order:

    - a key naming no 32-bit tag;
    - a rule mapping `_phi_rule_shape_refused` refuses, or an action the
      inspector does not implement;
    - `replacement:`, the old spelling of `value:`;
    - a `value:` under anything but REPLACE, or one that is not a string;
    - a repeating-group key (`60xx,xxxx`) under anything but REMOVE or
      KEEP;
    - Patient ID under REMOVE, EMPTY, SHIFT or JITTER, or with a
      `value:`: it can only be kept or pseudonymised;
    - Study or Series Instance UID under REMOVE, EMPTY, or REPLACE with a
      `value:`: the owner's stamp would export the source UID (#877);
    - REPLACE with `value: ''` on any other UI tag: the scan would propose
      nothing a UI holds, and the export would carry the source UID (#883);
    - SHIFT or JITTER on a standard tag that is not DA, DT or a sequence;
    - REPLACE on a standard tag whose VR cannot hold what it writes (a
      `value:`, else the VR's dummy, else `ANONYMIZED`). Value-less
      REPLACE on UI and on Study Date is exempt;
    - a REPLACE `value:` that holds a range (`-` in a DA or TM; in a DT,
      anywhere but the UTC offset), a `\\` on a tag of multiplicity 1, or
      a count of `\\`-separated values the dictionary VM does not allow.

    Args:
        tag: The rule's key, lowercased by the caller.
        rule: A display name (`str`) or a rule mapping; anything else is
            `_validated_phi_tags`' to refuse and gives None here.

    Returns:
        Optional[str]: The refusal, or None.
    """
    # The action check repeats `_validated_phi_tags`' because
    # `PhiInspector(config_tags=)` and `configuration.phi_tags` assigned
    # directly reach the scan without the loader. The order is the one the
    # messages are tested in; the shape check is first so a misspelt
    # `actoin:` is named rather than judged as the REPLACE it would default
    # to. An oversized key is refused as the loader refuses a non-tag key,
    # rather than letting pydicom raise `OverflowError`.
    # A malformed private-creator key is named on every door, not only
    # the loader's: assigned in code it would otherwise match nothing,
    # silently (#1132).
    creator = _creator_key_refused(tag)
    if creator is not None:
        return creator
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
                    f"'value' (renamed in 0.9.8), so rename it")
        if action == "SCRUB" and rule.get("target") is None:
            return (f"phi_tags[{tag!r}] is SCRUB with no target; name the "
                    f"kinds to remove, a list of {', '.join(SCRUB_KINDS)}")
        if rule.get("target") is not None and action != "SCRUB":
            return (f"phi_tags[{tag!r}] has a target under {action}; only "
                    f"SCRUB takes a target")
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
    if _is_creator_key(tag):
        # A private element, decided here rather than by the dictionary
        # reads below happening to find no entry for the key: a creator
        # key loads under every action a concrete private key does
        # (#1132).
        return None
    if _is_group_mask_key(tag):
        # Here, not only in `_validated_phi_tags`: `set_phi_tag`,
        # `configuration.phi_tags` and `PhiInspector(config_tags=)` never
        # reach that one. After the shape checks and before anything reads
        # the dictionary, which has no entry for a mask. A mask names
        # elements of many VRs, so an action whose effect depends on the VR
        # could not say what it writes.
        if action in ("REMOVE", "KEEP"):
            return None
        return (f"phi_tags[{tag!r}] is {action}; a repeating-group key (50xx "
                f"or 60xx) takes REMOVE or KEEP, because it names elements "
                f"of many VRs")
    # The ID is what keeps two patients apart, and `anonymize()` merges
    # patients that share one, so an emptied or literal ID would merge
    # every patient.
    if tag == _PATIENT_ID and (action in ("REMOVE", "EMPTY", "SHIFT", "JITTER",
                                          "SCRUB")
                               or value):
        said = f" with value {value!r}" if value else ""
        return (f"phi_tags['{tag}'] is {action}{said}; Patient ID can only be "
                f"kept (KEEP) or replaced by its keyed pseudonym (REPLACE with "
                f"no value), because the ID is what keeps two patients apart "
                f"and anonymize() merges patients that share one")
    # Refused rather than honoured or read as REPLACE (owner rulings on
    # #877): neither element can be absent from a valid file, and the
    # owner's stamp is what the export writes, so no REMOVE, EMPTY or
    # `value:` on either was ever applied; one literal would also merge
    # every study or series under it. Before the VR check, which passes a
    # value a UI can hold. SHIFT and JITTER fall to #559's arm below.
    # `is not None`, not truthiness: `value: ''` is a value here, because
    # the instance scan takes the keyed branch only for a None value and
    # proposes ANONYMIZED for '', which left a nested copy of the source
    # UID in the export (review of #881). Only null or no key loads.
    if tag in _OWNED_UIDS and (action in ("REMOVE", "EMPTY")
                               or (action == "REPLACE" and value is not None)):
        name, owner = _OWNED_UIDS[tag]
        valued = value is not None
        said = f" with value {value!r}" if valued else ""
        under = f"{action} with a value" if valued else action
        return (f"phi_tags['{tag}'] is {action}{said}; {name} can only be "
                f"kept (KEEP) or replaced by this project's keyed "
                f"replacement UID (REPLACE with no `value:` key), "
                f"because the {owner} writes its UID on every exported file, "
                f"so under {under} the export would carry the source UID")
    # Its own arm, before the UI exemption below, which reads `not value`
    # and so would let `''` through as the keyed rule; and not reached by
    # making that exemption `value is None`, which would fall through to
    # the VR check and say `''` writes `ANONYMIZED`, which is not what the
    # user wrote. Everywhere else `''` is no value; the instance scan's UI
    # branch alone read it as a value (`rule_value is None`), proposed
    # ANONYMIZED, had it declined (#560), and exported the source UID
    # (owner rulings Q2 A and Q3 A on #883). `set_phi_tag(value='')`
    # stores no `value:` key, so it never reaches here.
    if action == "REPLACE" and value == "" and _standard_dictionary_vr(tag) == "UI":
        return (f"phi_tags['{tag}'] is REPLACE with value ''; on a UI tag an "
                f"empty value: is still a value, and no UID is empty, so the "
                f"scan would propose nothing a UI can hold and the export "
                f"would carry the source UID. Omit the value: key (or write "
                f"value: null) for this project's keyed replacement UID, "
                f"or use EMPTY or REMOVE")
    if action == "SCRUB":
        # Text a description carries (#1130). A concrete private tag loads,
        # as a creator key did above: its value is read as text where it
        # is text. Patient ID, LO, was refused above; the other owned tags
        # are PN, DA and UI, refused here by their VR.
        try:
            private = int(tag[:4], 16) % 2 == 1
        except ValueError:
            private = False
        vr = None if private else _standard_dictionary_vr(tag)
        if private or vr in _SCRUB_VRS:
            return None
        said = f"{tag} is {vr}" if vr else f"{tag} is not in the dictionary"
        return (f"phi_tags['{tag}'] is SCRUB, and {said}; SCRUB removes "
                f"identifiers from text and applies only to LO, LT, SH, ST, "
                f"UC and UT, and to private tags; use REMOVE, EMPTY or KEEP"
                + (", or REPLACE for a name" if vr == "PN" else ""))
    if action in ("SHIFT", "JITTER"):
        # Otherwise it would decline on every pass. A sequence is exempt as
        # it is from REPLACE: the scan warns that the action has no meaning
        # there and applies nothing.
        vr = _standard_dictionary_vr(tag)
        if vr is not None and not {"DA", "DT", "SQ"} & set(vr.split(" or ")):
            return (f"phi_tags['{tag}'] is {action}, and {tag} is {vr}; "
                    f"SHIFT and JITTER move a date by the patient's offset "
                    f"and apply only to DA and DT")
        return None
    if action == "REPLACE" and not value and _standard_dictionary_vr(tag) == "UI":
        # The keyed UID replacement, which a UI holds by construction.
        return None
    if action == "REPLACE" and not (tag == _STUDY_DATE and not value):
        # With no `value:`, the VR's dummy: the same spelling as the
        # scan's, so the loader judges exactly what the scan will write.
        # Study Date's value-less REPLACE is the shift, not a literal.
        written = value or _vr_dummy(tag) or "ANONYMIZED"
        vr = _dictionary_vr_refuses(tag, written)
        if vr is not None:
            advice = "EMPTY or REMOVE"
            # Only for a `value:`: a value-less REPLACE on a DA or DT
            # writes its dummy and is never refused here.
            if vr in ("DA", "DT"):
                advice += ", or JITTER to shift it"
            # By arm: `US or SS` is as numeric as `US`.
            if not set(vr.split(" or ")) <= _NON_STRING_VRS:
                advice += f", or give a value: that is a valid {vr}"
            uid = ("; REPLACE with no value gives it this project's "
                   "replacement UID") if vr == "UI" else ""
            return (f"phi_tags['{tag}'] is REPLACE, which writes {written!r}, "
                    f"and {tag} is {vr}, which cannot hold it; use {advice}"
                    f"{uid}")
        # Ranges and multiplicity pydicom's `validate_value` passes. Only a
        # `value:` is counted: REPLACE with no value writes one value.
        if value:
            vr = _standard_dictionary_vr(tag)
            if vr in ("DA", "TM") and "-" in value:
                return (f"phi_tags['{tag}'] is REPLACE, which writes "
                        f"{value!r}, and a '-' in a {vr} is a range, which "
                        f"{tag} cannot hold; give one {vr} value")
            if vr == "DT" and _dt_is_a_range(value):
                return (f"phi_tags['{tag}'] is REPLACE, which writes "
                        f"{value!r}, and a '-' in a DT anywhere but its UTC "
                        f"offset (&ZZXX at the end) is a range, which {tag} "
                        f"cannot hold; give one DT value")
            vm = _dictionary_vm(tag)
            if "\\" in value and vm == "1":
                return (f"phi_tags['{tag}'] is REPLACE, which writes "
                        f"{value!r}, and {tag} holds one value, which a '\\' "
                        f"would make two; give a value without one")
            count = value.count("\\") + 1
            if vm is not None and not _vm_allows(vm, count):
                return (f"phi_tags['{tag}'] is REPLACE, which writes "
                        f"{value!r}, and {tag} holds {vm} values (its "
                        f"dictionary VM), which {count} '\\'-separated values "
                        f"are not; give a value of that multiplicity")
    return None


def validate_phi_policy(tags: Dict[str, Any], source: str) -> None:
    """Refuse the first rule in `tags` the pipeline cannot honour.

    Call it on the merged policy, before anything changes: a user's KEEP
    over an external profile's REMOVE on Patient ID is an honourable
    policy, and the profile alone is not.

    Args:
        tags (Dict[str, Any]): The policy; anything but a dict is
            accepted without a check.
        source (str): Named at the start of the refusal (a path, or the
            calling method).

    Raises:
        ValueError: For the first rule `_refused_phi_rule` refuses, then
            for two creator keys naming one creator with different rules
            (`_creator_twins_refused`, #1147).
    """
    # Every door a policy comes in by calls this: the merged policy a
    # config file resolves to (`ConfigLoader.load_unified_config`, so
    # `load_config` and `audit(config_path=)`), `set_phi_tag`, `audit()`
    # over `configuration.phi_tags` before a project secret is minted, and
    # `PhiInspector.__init__`.
    if not isinstance(tags, dict):
        return
    for tag, rule in tags.items():
        key = _canonical_rule_key(tag) if isinstance(tag, str) else tag
        reason = _refused_phi_rule(key, rule)
        if reason is not None:
            raise ValueError(f"{source}: {reason}")
    reason = _creator_twins_refused(tags)
    if reason is not None:
        raise ValueError(f"{source}: {reason}")


def _rule_apart_from_name(rule) -> Dict[str, Any]:
    """What a rule does, without its `name`: the action as the scan reads
    it (upper-cased, `REPLACE` when absent or for a bare-name string),
    `value: null` read as no `value` (`REPLACE` writes the VR's dummy
    under either, though the policy fingerprint tells them apart), a `SCRUB`
    `target` as a set of kinds, and every other key as written.

    A target's order decides only which kind a span two kinds found is
    counted for in the row, never what is removed; and of two twin keys
    the first one's rule is the one applied (`privacy._creator_index`),
    so its order is the one the rows follow.
    """
    if not isinstance(rule, dict):
        return {"action": "REPLACE"}
    done = {k: v for k, v in rule.items() if k not in ("name", "action")}
    if done.get("value", 0) is None:
        del done["value"]
    target = done.get("target")
    if (isinstance(target, (list, tuple))
            and all(isinstance(kind, str) for kind in target)):
        done["target"] = frozenset(target)
    action = rule.get("action", "REPLACE")
    done["action"] = action.upper() if isinstance(action, str) else action
    return done


def _creator_key_identity(key: str):
    """`(group, creator identity, offset)`: the element a creator key names."""
    group, creator, offset = _creator_key_parts(key)
    return group, _creator_identity(creator), offset


def _creator_twins(tags: Dict[str, Any], key: str) -> List[str]:
    """The keys of `tags`, as held, that name the element creator key `key`
    names under another spelling of its creator (#1147). A key that is
    `key` itself in another hex case is not a twin: it is one key."""
    identity = _creator_key_identity(key)
    canonical = _canonical_rule_key(key)
    return [tag for tag in tags
            if isinstance(tag, str) and _is_creator_key(tag)
            and _canonical_rule_key(tag) != canonical
            and _creator_key_identity(tag) == identity]


def _creator_twins_refused(tags: Dict[str, Any]) -> Optional[str]:
    """Why two creator keys of `tags` that name one creator cannot both
    stand, or None (#1147, owner ruling Q2 (a)).

    Two keys whose group, offset and `_creator_identity` agree name one
    element. With rules that act differently (`_rule_apart_from_name`),
    which one applied would
    be a silent precedence between, say, a KEEP and a REMOVE: #1147's
    shape again. Rules equal apart from `name` load, and the first key in
    the policy names the finding (`privacy._creator_index`). Keys are
    taken in their canonical spelling first, the later of two spellings of
    one key winning, as `PhiInspector._normalize_tag_keys` does, so a
    hex-case twin is that method's collision WARNING, never this refusal.

    Args:
        tags (dict): The policy.

    Returns:
        Optional[str]: The refusal, naming both keys, or None.
    """
    canonical = {}
    for tag, rule in tags.items():
        if isinstance(tag, str) and _is_creator_key(tag):
            canonical[_canonical_rule_key(tag)] = rule
    first = {}
    for key, rule in canonical.items():
        identity = _creator_key_identity(key)
        if identity not in first:
            first[identity] = key
            continue
        earlier = first[identity]
        if _rule_apart_from_name(canonical[earlier]) != _rule_apart_from_name(rule):
            # The fold is #1147's; the text names the rule, not the issue
            # (runtime strings carry no issue numbers, #811).
            return (f"phi_tags keys {earlier!r} and {key!r} name the same "
                    f"private element with rules that act differently: a "
                    f"creator key matches its creator whatever its case and "
                    f"runs of spaces (PS3.5 7.8.1 gives no rule for comparing "
                    f"two creators); give both keys one rule, or keep one key")
    return None


def load_unified_config(path: str) -> Dict[str, Any]:
    """Load a unified configuration file (YAML) and merge its policy.

    The root must be a mapping (a root-level list is refused), its
    `version` one this library reads and its top-level keys the
    schema's. Merges the file's `phi_tags` over its `privacy_profile`
    (built-in or external), or over the floor policy when the file has no
    `privacy_profile` line; `privacy_profile: none` makes the file's own
    `phi_tags` the whole policy.

    Args:
        path (str): Path to the YAML configuration file.

    Returns:
        Dict[str, Any]: The loaded configuration dictionary.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file does not end in `.yaml`/`.yml`, is not
            valid YAML, or fails any check above or in the `phi_tags` and
            profile validation, including a `privacy_profile` that is
            neither a shipped profile, `none`, nor an existing file.
    """
    return _loaded_unified_config(path)[0]


def _loaded_unified_config(path: str):
    """`load_unified_config`'s body: the configuration and its policy base.

    The base is what the policy was built on -- a built-in profile's pinned
    name, an external profile's path, `profiles.FLOOR` for a file with no
    `privacy_profile` line, or None for `privacy_profile: none` (and for an
    external profile that contributed no rules).
    """
    # The base is returned beside the dict rather than stored in it, so the
    # floor's sentinel never becomes a value of a configuration mapping.
    if not (path.endswith('.yaml') or path.endswith('.yml')):
        raise ValueError("Configuration file must be a YAML file (.yaml or .yml)")

    # Through `_load_yaml`, so a missing file is `FileNotFoundError` and a
    # syntax error is `ValueError("Invalid YAML format ...")`, never a raw
    # `yaml.parser.ParserError`.
    config = ConfigLoader._load_yaml(path)
    if not isinstance(config, dict):
        # An empty file (None), a scalar or a list at the root, each of
        # which would otherwise escape as a TypeError or AttributeError
        # from the first `.get` below.
        raise ValueError(
            f"{path}: a configuration must be a YAML mapping at its root "
            f"(privacy_profile, phi_tags, machines, ...), got "
            f"{type(config).__name__}")

    # First after the root is known to be a mapping: before the
    # keys, and before a profile file is opened, so a file this library
    # does not read is refused for that and nothing else.
    _checked_top_level(config, path)
    return _resolved_policy(config, path)


def _unshipped_profile_refusal(profile_name: str, path: str) -> ValueError:
    """The refusal for a `privacy_profile` holding `@` that names no
    profile this version ships.

    Args:
        profile_name (str): The name as written.
        path (str): Where it was written, for the message.

    Returns:
        ValueError: The refusal, for the caller to raise.
    """
    aliases = ", ".join(f"{alias!r} means {pinned}"
                        for alias, pinned in sorted(PROFILE_ALIASES.items()))
    return ValueError(
        f"{path}: privacy_profile {profile_name!r} is not a profile this "
        f"isocenter ships; it ships {', '.join(sorted(PRIVACY_PROFILES))} "
        f"({aliases}). A later PS3.15 edition arrives as a new name in a "
        f"newer isocenter")


def _resolved_policy(config: Dict[str, Any], path: str):
    """`config` with `phi_tags` validated and merged over its profile (or
    the floor), and the policy base (`_loaded_unified_config`); the body of
    `load_unified_config` after the top-level checks.

    Args:
        config (Dict[str, Any]): The file's root mapping; changed in place.
        path (str): The file's path, for refusals.

    Returns:
        tuple: `(config, base)`.

    Raises:
        ValueError: For refused `phi_tags`, an unshipped `@` name, or a
            profile that is neither built in, `none`, nor a file. Also
            propagated from `_external_profile_tags` for an external
            profile file it refuses (invalid YAML, a root that is not a
            mapping, no `phi_tags`, a refused version, another key, or
            refused `phi_tags`).
        FileNotFoundError: Propagated from `_external_profile_tags` when
            the external profile file is gone by the time it is read.
    """
    # Validated, and lowercased, before anything is merged. The profiles'
    # keys are lowercase (profiles.py's header comment), so a user's
    # `0008,103E` merged as spelled would sit beside the profile's
    # `0008,103e` as a second rule for one tag, with the later entry
    # winning by dict order and the report counting both.
    config["phi_tags"] = _validated_phi_tags(config.get("phi_tags"), path)

    # A null `privacy_profile:` is an absent one: the floor. Popped
    # here, so the floor branch below is the one place that decides it.
    if "privacy_profile" in config and config["privacy_profile"] is None:
        config.pop("privacy_profile")

    # `privacy_profile: none`: the file's `phi_tags` are the
    # whole policy, with no base beneath them.
    if "privacy_profile" in config and _names_no_profile(config["privacy_profile"]):
        config.pop("privacy_profile")
        return config, None

    # No `privacy_profile` line: the floor policy beneath the file's tags.
    # A loaded config extends or overrides what a bare session applies
    # rather than replacing it with its own few tags -- otherwise a one-tag
    # file switches the floor off by accident (Study ID and Institution
    # Name back in the export). `action: KEEP` opts one tag out;
    # `privacy_profile: none` opts out of the floor entirely.
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

    # 1. Built-in profiles, by pinned name or by a bare alias. The
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
    # reopens that for the next name it does not match.
    elif isinstance(profile_name, str) and "@" in profile_name:
        raise _unshipped_profile_refusal(profile_name, path)

    # 3. External File (Custom Profile). Its failures propagate: carrying
    # on with no profile would load a policy nobody wrote.
    elif isinstance(profile_name, str) and os.path.isfile(profile_name):
        profile_rules = _external_profile_tags(profile_name)
        get_logger().info("Loaded custom privacy profile from '%s' with %d rules.", profile_name, len(profile_rules))

    else:
        # A misspelt profile is refused, not warned about and dropped:
        # dropping it would load the file's own tags with no base beneath
        # them, a policy nobody wrote, behind a warning in front of a run
        # that then succeeds.
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
    `IsocenterConfiguration.save()` diffs `phi_tags` against.

    The inverse of `_resolved_policy`. An external profile is re-read
    now, not snapshotted at load.

    Args:
        privacy_profile (Optional[str]): The configuration's profile.
        floor (bool): Whether no profile means the floor.

    Returns:
        Dict[str, Any]: A built-in's table (the module's own: read it,
        never write it); an external file's `phi_tags` as the loader
        reads them; `FLOOR_POLICY` for no profile and `floor`; else `{}`
        (`none`, or an external profile that contributed nothing).

    Raises:
        ValueError: For a `privacy_profile` that is not a string, an
            unshipped `@` name, or neither a built-in profile nor an
            existing file; the last names the working directory for a
            relative path.
    """
    # Re-read, because the next load reads the file as it is on disk, so a
    # diff against it now is the one that reloads to memory when the
    # profile has changed or dropped a rule since. A profile that has gone
    # is refused now rather than at the next load; the message names where
    # a relative path was looked for, because under `auto_save` every
    # change method raises it, and the usual cause is a `chdir` since the
    # load.
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
        f"moved, set privacy_profile to its path")


def _refused_match(match, label) -> None:
    """Refuse a `match:` selector that cannot be compared as written (#1108).

    Args:
        match: The rule's `match` value.
        label (str): The rule's label, for the message.

    Raises:
        ValueError: In this order: not a mapping; an unknown key; no key;
            a value that is not a string (null included); a blank value.
    """
    if not isinstance(match, dict):
        raise ValueError(
            f"{label}: 'match' must be a mapping of manufacturer and/or "
            f"model_name, got {match!r} ({type(match).__name__})")
    reason = _unknown_keys(match, _MATCH_KEYS, "", "A match's")
    if reason is not None:
        raise ValueError(f"{label}: {reason}")
    if not match:
        raise ValueError(
            f"{label}: 'match' names no field; give manufacturer and/or "
            f"model_name, as the files spell them")
    for key in sorted(match):
        value = match[key]
        # Null is not absent here: leaving the key out is how a field is
        # left unmatched, and a null would compare with nothing.
        if not isinstance(value, str):
            raise ValueError(
                f"{label}: match {key!r} must be a string, got {value!r} "
                f"({type(value).__name__})")
        if not value.strip():
            raise ValueError(
                f"{label}: match {key!r} {value!r} is blank; leave the key "
                f"out to match any {key}")


class ConfigLoader:
    """Loads and validates unified YAML configuration files (schema
    version 2), through static methods.

    One door reads a configuration file, `load_unified_config`.
    """
    # Do not add a second loader: a partial reader (only `machines`, or
    # `phi_tags` with no profile merged and no `validate_phi_policy`)
    # accepts files the full one refuses.

    @staticmethod
    def load_unified_config(
            filepath: str) -> tuple[Dict[str, Any], List[Dict[str, Any]],
                                    Dict[str, Any], bool, Optional[str],
                                    Optional[Dict[str, Any]]]:
        """Parse a unified YAML configuration (v2) into its parts.

        Every check runs before this returns, so a caller that assigns
        only what it returns leaves its configuration unchanged when a
        file fails.

        Args:
            filepath (str): Path to the config file.

        Returns:
            tuple: (phi_tags, machine_rules, date_jitter_config,
            remove_private_tags, policy_base, burned_in_text).
            `date_jitter_config` is
            `{min_days, max_days}`, `{-365, -1}` when the file has none;
            `remove_private_tags` defaults to True. The last element is
            what the policy was built on: a built-in profile's pinned
            name (`basic@2026c`, also when the file said `basic`), an
            external profile's path, `profiles.FLOOR` -- an object,
            compared with `is` -- for a file with no `privacy_profile`
            line, or None for `privacy_profile: none` and for an external
            profile that contributed no rules. `burned_in_text` is
            `{action, min_confidence}` with the default filled in, or None
            when the file holds no policy (#1108).

        Raises:
            FileNotFoundError: If the file does not exist.
            ValueError: For every refusal of `load_unified_config` (the
                module function), a merged policy `validate_phi_policy`
                refuses, a `machines` that is not a list of mappings, a
                machine rule `_validate_rule` refuses, a `date_jitter`
                that is not `{min_days: int, max_days: int}` with
                `min_days <= max_days`, a `remove_private_tags` that
                is not a bool, and a `burned_in_text` that
                `_refused_burned_in_text` refuses.
        """
        # The version, the top-level keys, the phi_tags and the profile.
        data, base = _loaded_unified_config(filepath)
        return ConfigLoader._checked_parts(data, filepath, base)

    @staticmethod
    def _checked_parts(
            data: Dict[str, Any], filepath: str,
            base: Any) -> tuple[Dict[str, Any], List[Dict[str, Any]],
                                Dict[str, Any], bool, Any,
                                Optional[Dict[str, Any]]]:
        """The rest of `load_unified_config`: the merged policy judged, the
        machines, `date_jitter` and `remove_private_tags` checked.

        Args:
            data (Dict[str, Any]): The resolved configuration.
            filepath (str): The file's path, for refusals.
            base: The policy base, passed through.

        Returns:
            tuple: As `ConfigLoader.load_unified_config`.

        Raises:
            ValueError: As `ConfigLoader.load_unified_config`, for every
                check after the policy is resolved.
        """
        # Everything below validates before it returns, and `load_config`
        # assigns only what this returns: a file that fails any check
        # leaves the session's configuration exactly as it was.
        phi_tags = data["phi_tags"]
        # The merged policy, whichever branch produced it (a built-in or
        # external profile beneath the file, the floor, or `none`), so a
        # profile row the file did not override is judged and a file's
        # KEEP over a profile's refused row is not.
        validate_phi_policy(phi_tags, filepath)
        # `machines` only: `machine_rules` is refused by name at the top
        # level (`_checked_top_level`).
        machine_rules = data.get("machines", [])
        if machine_rules is None:
            machine_rules = []
        if not isinstance(machine_rules, list) or not all(
                isinstance(rule, dict) for rule in machine_rules):
            raise ValueError(
                f"{filepath}: 'machines' must be a list of rule mappings "
                f"(serial_number, redaction_zones, ...), got {machine_rules!r}")

        # Date Jitter. One shape is read, {min_days: int, max_days: int};
        # a bare int is refused with the mapping to write instead. A null
        # `date_jitter:` is the default, unlike a null
        # `remove_private_tags:` below: an absent range has one obvious
        # meaning here, and the default is what it gets.
        dj = data.get("date_jitter")
        reason = _refused_date_jitter(dj)
        if reason is not None:
            raise ValueError(f"{filepath}: {reason}")
        date_jitter_config = (dj if dj is not None
                              else {"min_days": -365, "max_days": -1})

        # A bool, and only a bool. Absent is True. Present and anything
        # else is refused rather than read for truth: `"false"` is a
        # non-empty string, which reads as true and would remove the
        # private tags the file asked to keep; `0`/`1` are refused by
        # `isinstance(..., bool)`, not `int`. A bare `remove_private_tags:`
        # (null) is refused too, although `phi_tags:` and `machines:` read
        # null as empty: a bool has no empty, and null reads as falsy --
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
                f"{why}")

        # Validate machines
        for i, rule in enumerate(machine_rules):
            ConfigLoader._validate_rule(rule, i)

        # A null `burned_in_text:` is absent, by owner ruling: a policy has
        # no empty, and the one reading of "no value" is "no policy".
        burned_in_text = _burned_in_text_policy(
            data.get("burned_in_text"), filepath)

        return (phi_tags, machine_rules, date_jitter_config,
                remove_private_tags, base, burned_in_text)

    @staticmethod
    def clean_filename(filename: str) -> str:
        """Sanitize a string for use as a filename.

        Args:
            filename (str): The text; any object is converted with `str`.

        Returns:
            str: Stripped, spaces replaced with underscores, and every
            character but word characters, `-` and `.` removed.
        """
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
        """Refuse a machine rule the redaction cannot read as written.

        Called by the loader, and by `add_rule`/`update_rule` before they
        store (and, with auto-save on, write) a rule.

        Args:
            rule (Dict[str, Any]): The machine rule.
            index (int): Its position, for the message.

        Raises:
            ValueError: In this order: an unknown key; a `serial_number`
                that is missing (null), not a `str`, or blank (whitespace
                only; `""` is the serial-less selector, #1108);
                `manufacturer`, `model_name` or `comment` present, not
                null and not a string; `redaction_zones` not a list; per
                zone, a zone neither a list nor a mapping, an unknown zone
                key, a `note` that is not a string, or an ROI that is not
                four non-negative integers (not bools) with start <= end
                on both axes.
        """
        # The order is load-bearing, and each step is pinned by a test:
        # unknown keys before the serial, so a misspelt `serial_numbr:` is
        # named rather than reported as a missing serial; `redaction_zones`
        # a list before its zones are walked, so `redaction_zones: 5` is a
        # `ValueError`, not a `TypeError`; a zone's keys before its ROI, so
        # a misspelt `rio:` is named rather than reported as a bad ROI.
        # Nothing reads `manufacturer`, `model_name` or `comment`; they are
        # checked so no key the loader accepts carries an unchecked type.
        sn = rule.get("serial_number")
        label = f"Rule #{index} ({sn})" if isinstance(sn, str) and sn.strip() else f"Rule #{index}"

        reason = _unknown_keys(rule, _RULE_KEYS, "", "A machine rule's")
        if reason is not None:
            raise ValueError(f"{label}: {reason}")

        # `""` is a selector, not a missing serial (#1108): it names every
        # series with no Device Serial Number, which is what ingest holds
        # for one. Null is still missing: YAML reads a bare
        # `serial_number:` as null, and the author meant something.
        if "match" in rule:
            # The other selector (#1108); exactly one per rule, so which
            # series a rule covers never depends on how two selectors in
            # one rule would combine.
            if "serial_number" in rule:
                raise ValueError(
                    f"{label}: a rule has exactly one of 'serial_number' and "
                    f"'match'; write two rules to select by both")
            _refused_match(rule["match"], label)
        elif sn is None:
            raise ValueError(
                f"Rule #{index}: Missing 'serial_number'; write "
                f"serial_number: \"\" (quoted) for the series with no Device "
                f"Serial Number, \"*\" for every series, or match: with "
                f"manufacturer and/or model_name to select by device.")
        elif not isinstance(sn, str):
            # An unquoted serial is a YAML number -- `0123` loads as the
            # octal 83 -- and never equals a Device Serial Number, so the
            # rule would match nothing and the machine would not be redacted.
            raise ValueError(
                f"Rule #{index}: 'serial_number' must be a string, got {sn!r} "
                f"({type(sn).__name__}); quote it as it is written on the "
                f"machine (serial_number: \"0123\", not serial_number: 0123), "
                f"because YAML reads unquoted digits as a number, and a "
                f"leading 0 as octal: 0123 loads as 83")
        elif sn and not sn.strip():
            # No Device Serial Number is blank (ingest holds an absent one
            # as `""`), so the rule would match no machine and redact
            # nothing.
            raise ValueError(
                f"Rule #{index}: 'serial_number' {sn!r} is blank; give the "
                f"serial as it is written on the machine, or write "
                f"serial_number: \"\" for the series with no Device Serial "
                f"Number")

        for key in ("manufacturer", "model_name", "comment"):
            # Null is absent (the comment above `_VERSION_SHAPE`).
            if rule.get(key) is not None and not isinstance(rule[key], str):
                raise ValueError(
                    f"{label}: '{key}' must be a string, got {rule[key]!r} "
                    f"({type(rule[key]).__name__})")

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
                        f"{zone['note']!r} ({type(zone['note']).__name__})")
                roi = zone.get("roi")
            else:
                raise ValueError(
                    f"Rule #{index} ({sn}), Zone #{z_idx}: Invalid zone format (must be list or dict).")

            # The integer check is part of the shape check: a zone of
            # strings would otherwise reach the `x < 0` below and escape as
            # a TypeError.
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

"""
Configuration manager for handling Isocenter system settings.

This module provides functionality to load, validate, and manage configuration
files for the Isocenter application. It supports unified YAML configurations,
legacy formats, and privacy profile management.
"""

import os
import logging
import copy
from typing import Dict, Any, List, Optional
import re
import yaml

from .profiles import FLOOR_POLICY, PRIVACY_PROFILES

CONFIG_VERSION = "2.0"

#: Where this package's own shipped resources live.
#:
#: Hoisted out of `load_phi_config`'s body in #388 so a test could
#: monkeypatch it. `load_phi_config` no longer reads a resource -- the
#: default PHI policy is `profiles.FLOOR_POLICY`, in Python, since #495
#: deleted `resources/phi_tags.json` -- and the constant stays because
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
    """True for `privacy_profile: none` and `privacy_profile: null`."""
    return profile_name is None or (
        isinstance(profile_name, str) and profile_name.strip().lower() == "none")


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


def _external_profile_tags(path: str) -> Any:
    """The `phi_tags:` mapping of an external profile file.

    A file with no `phi_tags` key had its root mapping used as the tags
    (`load_phi_config`'s legacy fallback), so a profile written as a
    config -- `privacy_profile: basic` and its rules at the top level --
    loaded `privacy_profile` itself as a "tag" (review of #509). Refused,
    naming the file; the value is validated by the caller.
    """
    data = ConfigLoader._load_yaml(path)
    if not isinstance(data, dict) or "phi_tags" not in data:
        raise ValueError(
            f"{path}: an external privacy profile must carry its rules under "
            f"a 'phi_tags:' mapping; this file has no phi_tags key")
    return data["phi_tags"]


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
        if not _is_tag_key(tag):
            raise ValueError(
                f"{source}: phi_tags key {tag!r} is not a 'gggg,eeee' tag "
                f"(four hex digits, a comma, four hex digits, such as "
                f"'0010,0010'); the scan reads no tag by that key, so the "
                f"rule would never run")
        if isinstance(rule, dict):
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


def _dictionary_vr_refuses(tag: str, value: Any) -> Optional[str]:
    """The dictionary VR of a **standard** tag when that VR cannot hold
    `value`, else None (#560).

    None as well for a private (odd-group) tag, an unknown tag, and a
    sequence: the exporter writes a private value its recorded VR cannot
    hold as a valid LO, so a refusal there would keep an identifier the
    write removes; an unknown tag has no VR to judge by; and a value on a
    sequence is warned about by the scan, not written.

    The verdict is pydicom's `validate_value`, not
    `io_handlers._value_fits_vr`, which skips repertoire by design and so
    passes `ANONYMIZED` for TM, DT and UI. Two things `validate_value`
    does not do are done here: AT is refused outright (pydicom does not
    validate an AT string, and the value is a tag), and a compound
    dictionary VR (`US or SS`, `OB or OW`) is split, because pydicom has
    no validator under that name and passes anything. It is refused when
    no arm holds the value.
    """
    from pydicom import config as pydicom_config  # pylint: disable=import-outside-toplevel
    from pydicom.valuerep import validate_value  # pylint: disable=import-outside-toplevel

    vr = _standard_dictionary_vr(tag)
    if vr is None:
        return None
    # A sequence needs no arm of its own: pydicom has no validator for SQ
    # and passes any value, and the scan warns about a value on one.
    arms = [arm.strip() for arm in vr.split(" or ")]

    def holds(arm):
        if arm == "AT":
            return False
        try:
            validate_value(arm, value, pydicom_config.RAISE)
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

    1. `replacement:` is 0.9.7's `set_phi_tag` spelling of `value:`,
       which nothing read (#538). One spelling, so it is refused by name.
    2. A `value:` under anything but REPLACE writes nothing.
    3. A `value:` that is not a string cannot be written as one.
    4. Patient ID can only be kept or pseudonymised: the ID is what keeps
       two patients apart, and `anonymize()` merges patients that share
       one (#548), so an emptied or literal ID would merge every patient.
    5. SHIFT/JITTER on a standard tag that is not DA or DT declined on
       every pass (#559).
    6. REPLACE on a standard tag whose VR cannot hold what it writes
       (#560). Study Date's REPLACE with no value is the shift (#537, Q3)
       and is not judged as a literal.
    """
    if isinstance(rule, dict):
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
    if action == "REPLACE" and not (tag == _STUDY_DATE and not value):
        written = value or "ANONYMIZED"
        vr = _dictionary_vr_refuses(tag, written)
        if vr is not None:
            advice = "EMPTY or REMOVE"
            if vr in ("DA", "DT"):
                advice += ", or JITTER to shift it"
            if vr not in _NON_STRING_VRS:
                advice += f", or give a value: that is a valid {vr}"
            return (f"phi_tags['{tag}'] is REPLACE, which writes {written!r}, "
                    f"and {tag} is {vr}, which cannot hold it; use {advice} "
                    f"(#560)")
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

    Supports legacy list-based config (machine rules only) and new dict-based config.
    Merges 'privacy_profile' if specified (Built-in or External).

    Args:
        path (str): Path to the YAML configuration file.

    Returns:
        Dict[str, Any]: The loaded configuration dictionary.

    Raises:
        ValueError: If file is not YAML.
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

    # Validated, and lowercased, before anything is merged. The profiles'
    # keys are lowercase (profiles.py's header comment), so a user's
    # `0008,103E` merged as spelled sat beside the profile's `0008,103e`
    # as a second rule for one tag: `PhiInspector` collapsed the pair at
    # scan time with the later entry winning by dict order, and the
    # report counted both (#495).
    config["phi_tags"] = _validated_phi_tags(config.get("phi_tags"), path)

    # `privacy_profile: none` (or `null`): the file's `phi_tags` are the
    # whole policy, with no base beneath them. The scaffold's header has
    # told users to write this "for manual control" since v2.0, and it
    # warned "Unknown privacy profile" and loaded nothing until #495.
    if "privacy_profile" in config and _names_no_profile(config["privacy_profile"]):
        config.pop("privacy_profile")
        return config

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
        return config

    # Merge Privacy Profile
    if "privacy_profile" in config:
        profile_name = config["privacy_profile"]

        profile_rules = {}

        # 1. Check Built-in Profiles
        if isinstance(profile_name, str) and profile_name in PRIVACY_PROFILES:
            profile_rules = copy.deepcopy(PRIVACY_PROFILES[profile_name])
            get_logger().info("Loaded built-in privacy profile '%s' with %d rules.", profile_name, len(profile_rules))

        # 2. Check External File (Custom Profile). Its failures propagate:
        # this logged "Failed to load custom profile" and carried on with
        # no profile, which is #456's silence one file further out.
        elif isinstance(profile_name, str) and os.path.isfile(profile_name):
            profile_rules = _validated_phi_tags(
                _external_profile_tags(profile_name), profile_name)
            get_logger().info("Loaded custom privacy profile from '%s' with %d rules.", profile_name, len(profile_rules))

        else:
            # A misspelt profile is refused, not warned about and dropped
            # (#456): the drop loaded the file's own tags with no base
            # beneath them, a policy nobody wrote, behind a warning in
            # front of a run that then succeeded. `comprehensive`, which
            # README and docs/configuration.md offered, never existed and
            # took this path.
            raise ValueError(
                f"{path}: privacy_profile {profile_name!r} is neither a "
                f"built-in profile ({', '.join(sorted(PRIVACY_PROFILES))}), "
                f"'none', nor an existing file")

        if not profile_rules:
            # An external profile with no tags contributed nothing; naming
            # it would let the compliance report describe protection that
            # never ran.
            config.pop("privacy_profile", None)

        # User rules override profile rules
        profile_rules.update(config["phi_tags"])
        config["phi_tags"] = profile_rules

    return config


class ConfigLoader:
    """
    Loads and validates configuration files for the Isocenter system.

    This class provides static methods to parse unified YAML configuration files (v2.0),
    legacy configuration formats, and PHI tag definitions. It handles configuration
    validation, normalization, and file I/O operations.

    Supports multiple configuration formats:
    - Unified v2.0 YAML configs with PHI tags, machine rules, and date jitter settings
    - Legacy machine rule configurations
    - PHI tag definitions (from files or internal defaults)

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
            remove_private_tags, privacy_profile). The last element is the
            name of the profile whose rules were merged, or None -- an
            unknown reference resolves to None rather than to its own name,
            because it contributed nothing.
        """
        # Call the top-level loader which handles YAML, Legacy List, and Privacy Profiles
        data = load_unified_config(filepath)

        # Everything below validates before it returns, and `load_config`
        # assigns only what this returns: a file that fails any check
        # leaves the session's configuration exactly as it was (#456).
        phi_tags = data["phi_tags"]
        # The merged policy, whichever branch produced it (a built-in or
        # external profile beneath the file, the floor, or `none`), so a
        # profile row the file did not override is judged and a file's
        # KEEP over a profile's refused row is not (#537, #560).
        validate_phi_policy(phi_tags, filepath)
        # Support 'machines' (v2) or 'machine_rules' (legacy internal)
        machine_rules = data.get("machines", data.get("machine_rules", []))
        if machine_rules is None:
            machine_rules = []
        if not isinstance(machine_rules, list) or not all(
                isinstance(rule, dict) for rule in machine_rules):
            raise ValueError(
                f"{filepath}: 'machines' must be a list of rule mappings "
                f"(serial_number, redaction_zones, ...), got {machine_rules!r}")

        # Date Jitter Normalization. Two shapes are read: an int, a fixed
        # shift (legacy), and {min_days: int, max_days: int}. Anything else
        # loaded until #456 and then failed in `load_config`'s own print,
        # after the assignments, leaving `date_jitter: soon` in the session.
        dj = data.get("date_jitter")
        if dj is None:
            date_jitter_config = {"min_days": -365, "max_days": -1}
        elif isinstance(dj, int) and not isinstance(dj, bool):
            date_jitter_config = {"min_days": dj, "max_days": dj}
        elif (isinstance(dj, dict) and set(dj) == {"min_days", "max_days"}
              and all(isinstance(v, int) and not isinstance(v, bool)
                      for v in dj.values())):
            date_jitter_config = dj
        else:
            raise ValueError(
                f"{filepath}: 'date_jitter' must be {{min_days: int, "
                f"max_days: int}} or a single int, got {dj!r}")

        remove_private_tags = data.get("remove_private_tags", True)

        # Validate machines
        for i, rule in enumerate(machine_rules):
            ConfigLoader._validate_rule(rule, i)

        return (phi_tags, machine_rules, date_jitter_config,
                remove_private_tags, data.get("privacy_profile"))

    @staticmethod
    def load_redaction_rules(filepath: str) -> List[Dict[str, Any]]:
        """
        Legacy/Convenience support for loading only Machine Rules.

        Use this if you only need the 'machines' list from a unified config,
        or an old-style legacy config file.

        Args:
            filepath (str): Path to the config file.

        Returns:
            List[Dict[str, Any]]: List of validated machine rule dictionaries.
        """
        data = ConfigLoader._load_yaml(filepath)

        rules = []

        if "machines" in data:
            rules = data["machines"]  # v1 or v2
        else:
            get_logger().warning("Config Warning: Could not find 'machines' list.")

        for i, rule in enumerate(rules):
            ConfigLoader._validate_rule(rule, i)

        return rules

    @staticmethod
    def load_phi_config(filepath: str = None) -> Dict[str, str]:
        """
        Legacy/Convenience support for loading only PHI Tags.

        Arg:
            filepath (str, optional): Path to config file. If None, returns
                a copy of the floor policy, `profiles.FLOOR_POLICY`.

        Returns:
            Dict: Mapping of tags to configuration (action/name).
        """
        if filepath:
            data = ConfigLoader._load_yaml(filepath)
            if not isinstance(data, dict):
                raise ValueError(
                    f"{filepath}: a PHI tag file must be a YAML mapping at its "
                    f"root, got {type(data).__name__}")

            # Support v2 unified file used as simple PHI config
            if "phi_tags" in data:
                return data["phi_tags"]
            return data.get("phi_tags", data)  # Fallback to assumes root dict is tags if no key
        # The default policy is the floor a bare session applies (#495).
        # It was `resources/phi_tags.json` -- six name-only tags, every
        # one of them already in the basic profile -- which only this
        # arm, the scaffold's tag names and the report's rule count ever
        # read, while `audit()` on a bare session scanned against `{}`:
        # the report named six rules the scan never ran. A copy, because
        # `PhiInspector` normalizes what it is handed and a caller may
        # edit it.
        return copy.deepcopy(FLOOR_POLICY)

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
        sn = rule.get("serial_number")
        if not sn:
            raise ValueError(f"Rule #{index}: Missing 'serial_number'.")

        zones = rule.get("redaction_zones", [])
        if not isinstance(zones, list):
            raise ValueError(f"Rule #{index} ({sn}): 'redaction_zones' must be a list.")

        for z_idx, zone in enumerate(zones):
            if isinstance(zone, list):
                roi = zone
            elif isinstance(zone, dict):
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

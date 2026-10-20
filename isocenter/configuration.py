"""
Defines the runtime configuration structures for the Isocenter application.

This module contains the `IsocenterConfiguration` dataclass which encapsulates everything
needed to drive a session's behavior, including redaction rules, PHI profiling,
and date shifting parameters. It also handles the persistent state of these
settings in a backing YAML file, which it writes only when asked:
`save()`, or after every change while `auto_save` is on.
"""
import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence
import yaml

from . import config_manager, profiles
from .entities import ScanPolicy
from .profiles import FLOOR_POLICY

#: `save()` with nowhere to write, and any change under `auto_save` with
#: nowhere to write. Raised, never a silent return: a method must not
#: report success after doing nothing.
_NO_FILE = ("configuration.save() has no file to write: load_config(path) "
            "sets one, or set session.configuration.config_path")


class FlowList(list):
    """A list YAML should render inline, as [a, b, c].

    Redaction zones read as coordinates, not as a bulleted list four
    lines tall.
    """


def _flow_list_representer(dumper, data):
    return dumper.represent_sequence(
        'tag:yaml.org,2002:seq', data, flow_style=True)


# Registered once, at import: registering it again per call mutates global
# PyYAML state each time.
yaml.add_representer(FlowList, _flow_list_representer)


# --- The policy a PHI status is recorded under -----------------------------
#
# **A store format.** Every status a 1.x store holds carries a `"v1:"`
# fingerprint made here, and a 1.0 store opens in every 1.x, so the v1
# canonical form is never changed: a later 1.x adds `_canonical_policy_v2`
# with a `"v2:"` prefix beside it, and compares a stored v1 record against
# the in-force policy's *v1* fingerprint. A stored fingerprint is never
# rewritten -- the input it hashed is gone, and deriving it again would be
# the back-fill the migration refuses. The bytes are pinned under a fixed
# `CONFIG_VERSION`: a minor bump moves every fingerprint by design and is
# not a change of the form.
#
# **The rule it keeps: it may tell apart two policies that scan alike, and
# must never equate two that scan differently.** Telling them apart costs a
# re-audit; equating them lets a stale status pass for current. So nothing
# is normalized. The inspector does not read a rule one way: `_owned_rule` reads
# `rule.get("action") or "REPLACE"` and the configured-tag scan
# `rule.get("action", "REPLACE")`, so `action: ""` is two different things
# at two sites and hashes as itself.
#
# In: every rule key except `name`, the rule's form,
# `remove_private_tags`, and `config_manager.CONFIG_VERSION`. Out: `name` and a
# bare-string rule's text, which only label a finding; `date_jitter`,
# which moves a shift and not what is flagged; the pixel rules; the
# project secret; the library version; and the base, which is a label.
#
# **What `CONFIG_VERSION` is doing here.** The dict is not the whole of
# what a scan does with it: a library change (a value-less REPLACE writing
# a VR dummy, a repeating-group mask key resolving) can make two identical
# dicts scan differently under one fingerprint -- the rule above, broken
# by the library rather than the config. A release that changes what an
# unchanged configuration does bumps `CONFIG_VERSION`'s minor, so the
# version names the behaviour the dict was read with, and a store's
# statuses from before the bump read as another policy at export. Not the
# library version: every release would then cost every store a re-audit,
# for releases that change nothing a scan reads. The cost of the version
# is the same in kind and rarer: a minor bumped for a key added, with no
# behaviour changed, tells apart two policies that scan alike. Every v1
# hash includes `CONFIG_VERSION`.


def _tagged(value):
    """A value JSON cannot hold, as text that says what it was.

    Args:
        value (Any): Any value.

    Returns:
        dict: `{"__type__": <type name>, "__str__": str(value)}`.
    """
    # `_scan_policy()` runs at export with no validator in front of it, over
    # a `phi_tags` code can assign, so the canonical form must never raise:
    # a rule value YAML read as a `date` hashes as that date, not as a
    # string that happens to spell it.
    return {"__type__": type(value).__name__, "__str__": str(value)}


def _key(key):
    # `json.dumps(default=)` never reaches keys, and `sort_keys` raises on
    # keys of mixed types, so a non-str key is replaced by its repr.
    return key if isinstance(key, str) else repr(key)


def _canonical_policy_v1(phi_tags, remove_private_tags) -> bytes:
    """The v1 canonical form of a tag policy. Never change it (see above).

    Args:
        phi_tags (dict): The tag policy. Rule `name` fields and a string rule's
            text are left out.
        remove_private_tags (bool): The private-tag switch.

    Returns:
        bytes: Sorted, compact ASCII JSON of the rules, the switch and
        `config_manager.CONFIG_VERSION`.
    """
    rules = {}
    for tag, rule in (phi_tags or {}).items():
        if isinstance(rule, dict):
            rules[_key(tag)] = {_key(k): v for k, v in rule.items()
                                if k != "name"}
        elif isinstance(rule, str):
            # A string rule is REPLACE labelled with the string, except
            # that the configured-tag scan skips an empty one
            # (`if not config_val: continue`) where `_owned_rule` does not:
            # so its text is out, and whether it is empty is in.
            rules[_key(tag)] = {"__form__": "string" if rule else "empty-string"}
        else:
            rules[_key(tag)] = {"__form__": _tagged(rule)}
    # Read through the module at call time, never bound by a `from`
    # import: the bump is what moves every fingerprint.
    doc = {"config_version": config_manager.CONFIG_VERSION,
           "phi_tags": rules, "remove_private_tags": bool(remove_private_tags)}
    return json.dumps(doc, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, default=_tagged).encode("ascii")


def _scan_policy_for(phi_tags, remove_private_tags, base: str) -> ScanPolicy:
    """The `ScanPolicy` a scan over `phi_tags` records, labelled `base`.

    Args:
        phi_tags (dict): The tag policy.
        remove_private_tags (bool): The private-tag switch.
        base: The policy base label.

    Returns:
        ScanPolicy: `"v1:"` plus the sha256 hex of the v1 canonical form,
        with `base`.
    """
    return ScanPolicy("v1:" + hashlib.sha256(
        _canonical_policy_v1(phi_tags, remove_private_tags)).hexdigest(), base)


def _policy_base_label(base) -> str:
    """The loader's fifth element as the label a person reads.

    Args:
        base: `profiles.FLOOR` for the floor, None for `privacy_profile:
            none`, or a pinned profile name or an external profile's path.

    Returns:
        str: `floor over <FLOOR_BASE>`, `none`, or `base` unchanged.
    """
    # The one spelling of each, shared by `IsocenterConfiguration._policy_base`
    # and `Session.audit(config_path=)`, so one base cannot be written two
    # ways.
    if base is profiles.FLOOR:
        return f"floor over {profiles.FLOOR_BASE}"
    if base is None:
        return "none"
    return base


#: What `(0012,0063)` calls a policy whose base is an external profile's
#: path: the path is the operator's directory layout, and it must not
#: reach exported data.
EXTERNAL_PROFILE_LABEL = "external profile"


def _deid_method_label(base: str) -> str:
    """A recorded `ScanPolicy.base` as De-identification Method names it:
    verbatim when it is one of the shapes this library spells --
    a pinned profile name, `none`, or the floor's label -- and
    `external profile` for anything else, which is a path.

    Exact matches only, so no string that merely looks like one of them
    (a pinned name in another case, a relative path beginning
    `floor over` and a space) is written verbatim.

    Args:
        base: A recorded `ScanPolicy.base`.

    Returns:
        str: `base`, or `EXTERNAL_PROFILE_LABEL`.
    """
    if (base in profiles.PRIVACY_PROFILES
            or base in (_policy_base_label(None),
                        _policy_base_label(profiles.FLOOR))):
        return base
    return EXTERNAL_PROFILE_LABEL


def _deid_method_value(policy: ScanPolicy, version: str) -> str:
    """This step's De-identification Method `(0012,0063)` value.

    `isocenter/<version>; <label>; v1:<8 hex>`: the label is
    `_deid_method_label` of the recorded base, and the hex is the first 8
    characters of `ScanPolicy.fingerprint`, never recomputed. The
    fingerprint carries `CONFIG_VERSION`, so a minor bump moves it in
    every exported file.

    Args:
        policy (ScanPolicy): The recorded policy.
        version (str): The library version.

    Returns:
        str: The value, untruncated: `isocenter/<version>; <label>;
        <scheme>:<first 8 hex of the digest>`.
    """
    # `isocenter/<version>` is the exact spelling the output fingerprint's
    # N2 substitution normalises (`scripts/output_fingerprint.py`), so a
    # release bump moves no recorded output; any other spelling would.
    # Eight hex characters, not more, for LO's 64: the floor's label with a
    # 17-character version is exactly 64.
    scheme, _, digest = policy.fingerprint.partition(":")
    return (f"isocenter/{version}; {_deid_method_label(policy.base)}; "
            f"{scheme}:{digest[:8]}")


@dataclass
class IsocenterConfiguration:
    """
    The configuration a session applies, reached as `session.configuration`.

    `load_config()` fills it from a file. The methods below change it in
    memory, and `save()` writes it.

    Attributes:
        rules (List[Dict[str, Any]]): List of machine redaction rules.
        phi_tags (Dict[str, Any]): PHI tag policies, keyed by lowercase
            `"gggg,eeee"` tag. With none given, it is a copy of the floor
            policy (`profiles.FLOOR_POLICY`). The floor policy is the
            PS3.15 `basic@2026c` profile plus three research defaults, and
            a session applies it before any configuration is loaded (see
            the Configuration guide). Each instance holds its own copy, so
            one session's `set_phi_tag` cannot reach the policy of another
            session or the module table.
        date_jitter (Dict[str, int]): The range, in days, from which the
            per-patient date offset is derived. Both ends are included.
            The default is `{"min_days": -365, "max_days": -1}`.
        remove_private_tags (bool): Global flag to strip private tags.
        config_path (Optional[str]): The file that `save()` writes.
            `load_config()` sets it, or you can set it by hand.
        auto_save (bool): If True, every `add_rule`, `update_rule`,
            `delete_rule` and `set_phi_tag` writes `config_path`. The
            default is False.
        privacy_profile (Optional[str]): The pinned name of the built-in
            profile whose rules were merged into `phi_tags`, or an external
            profile's path, or None. The pinned name is `basic@2026c`, also
            when the file said `basic`. None means that no named profile
            was applied: the floor, or `privacy_profile: none`. It is only
            ever set to a profile that resolved.
    """
    rules: List[Dict[str, Any]] = field(default_factory=list)
    phi_tags: Dict[str, Any] = field(
        default_factory=lambda: copy.deepcopy(FLOOR_POLICY))
    date_jitter: Dict[str, int] = field(default_factory=lambda: {"min_days": -365, "max_days": -1})
    remove_private_tags: bool = True
    config_path: Optional[str] = None
    privacy_profile: Optional[str] = None
    #: Off by default: a loaded file is the user's, and a save rewrites it
    #: whole, dropping its comments and layout.
    #: Survives `load_config()`, which never assigns it: it is the
    #: session's choice, not the file's, and a later load is written to.
    auto_save: bool = False
    #: The `burned_in_text` policy (#1108): `{"action": "report" |
    #: "redact" | "withhold", "scope": "flagged" | "modalities" | "all",
    #: "min_confidence": 0..100, "target": "any" | [kinds]}`, plus a
    #: sorted `"modalities"` list under `scope: modalities` (#1121), or
    #: None for no OCR anywhere. Tier 2. Set by `load_config()` on every load, with
    #: the default confidence filled in; assigned by hand, it is judged
    #: by the loader's rule where it is used. Not in the PHI policy
    #: fingerprint, as zones are not. Not a constructor parameter, so the
    #: frozen field list and `__init__` are unchanged.
    burned_in_text: Optional[Dict[str, Any]] = field(default=None, init=False)
    # Whether `phi_tags` came from the floor policy: a bare configuration,
    # or a loaded file with no `privacy_profile` line. `Session.load_config`
    # sets it on every load. The floor and `privacy_profile: none` both
    # leave `privacy_profile` at None, and nothing else here tells them
    # apart. Private and not a constructor parameter, so the frozen field
    # list is unchanged.
    _floor: bool = field(default=True, init=False, repr=False, compare=False)
    # Whether `config_path` holds what memory holds, as far as this object
    # knows: cleared by the first change that stays in memory, set by a
    # save that succeeded and by `Session.load_config`. It exists for the
    # one-line notice, so a script that expects its file to follow its
    # edits is told once that it does not.
    _file_in_sync: bool = field(default=True, init=False, repr=False, compare=False)

    @property
    def _policy_base(self) -> str:
        """What the policy in force was built on, as one string.

        Returns:
            str: The pinned profile name or external path, `floor over
            basic@2026c`, or `none`: the identifier the report prints
            and the store's policy record carries.
        """
        if self.privacy_profile:
            return self.privacy_profile
        return _policy_base_label(profiles.FLOOR if self._floor else None)

    def _scan_policy(self) -> ScanPolicy:
        """The policy in force: what `audit()` with no argument scans with.

        Returns:
            ScanPolicy: The fingerprint of `phi_tags` and
            `remove_private_tags` as they stand, labelled
            `_policy_base`.
        """
        # Computed on every call, never cached: `phi_tags` and
        # `remove_private_tags` can be assigned directly.
        return _scan_policy_for(self.phi_tags, self.remove_private_tags,
                                self._policy_base)

    def save(self) -> None:
        """
        Write the configuration to `config_path` as YAML.

        The file names the profile and does not copy the profile's rules.
        `privacy_profile` is the pinned name (`basic@2026c`), the external
        profile's path or `none`. For the floor, the file has no
        `privacy_profile` line. `phi_tags` holds only the rules that differ
        from the rules of that base.

        Then the file holds `date_jitter`, `remove_private_tags` and every
        machine rule, with each key kept (`comment:` as data). `version` is
        always written, as this library's configuration version. A file
        that this method writes loads to the configuration that it was
        written from.

        This method writes a new file, so it does not keep the comments
        and layout of an existing file.

        Raises:
            ValueError: If there is no `config_path`. If `phi_tags` has no
                rule for a tag that its base supplies, because a file that
                names that base would bring the rule back on reload. If
                `date_jitter` is a range that the loader would refuse
                (#731). If `config_path` is a file that declares a newer
                `version` minor than this library's (#784). If
                `burned_in_text` holds a policy that the loader would
                refuse in a file (#1108). In each case, nothing is written.
            OSError: The write's own error, unchanged.
        """
        if not self.config_path:
            raise ValueError(_NO_FILE)
        # Rendered before the file is opened, so a refusal or a rendering
        # failure cannot leave it truncated. Then written in place rather
        # than through a temporary file and `os.replace`, which would turn
        # a symlinked config into a regular file and drop its mode.
        text = self._rendered()
        config_manager._refuse_overwriting_a_newer_minor(self.config_path)
        with open(self.config_path, "w", encoding="utf-8") as handle:
            handle.write(text)
        self._file_in_sync = True

    def _rendered(self) -> str:
        """The YAML `save()` writes.

        Returns:
            str: The document.

        Raises:
            ValueError: When `phi_tags` lacks a rule its base supplies, or
                `date_jitter` is a range the loader refuses (#731), or
                `burned_in_text` is a policy the loader refuses (#1108).
        """
        # First, before the file is opened: a range the loader refuses
        # would make a file that does not load (#731). Under auto-save this
        # refuses every mutator while the range is wrong, which is right:
        # the file each would write is that file.
        config_manager._refuse_date_jitter_in_code(self.date_jitter)
        # The base lookup sits beside the loader's resolution, so the two
        # cannot resolve a name differently.
        overrides = self._phi_tags_over(
            config_manager._policy_base_rules(self.privacy_profile, self._floor))

        machines = []
        for rule in self.rules:
            rule_copy = rule.copy()
            if "redaction_zones" in rule_copy:
                # One flow list is enough: PyYAML writes everything inside
                # a flow collection in flow style, zones and `roi`s alike.
                rule_copy["redaction_zones"] = FlowList(rule_copy["redaction_zones"])
            machines.append(rule_copy)

        data = {
            # The loader's constant, read at call time: one home for the
            # number both writers stamp.
            "version": config_manager.CONFIG_VERSION,
        }
        # No line for the floor: an absent line means the floor in every
        # 1.x. `none` would mean "exactly these rules", not the floor.
        if self.privacy_profile:
            data["privacy_profile"] = self.privacy_profile
        elif not self._floor:
            data["privacy_profile"] = "none"
        data["phi_tags"] = overrides
        data["date_jitter"] = self.date_jitter
        data["remove_private_tags"] = self.remove_private_tags
        data["machines"] = machines
        # Only when set: a file with no policy carries no line, so a
        # library from before the key still loads what this one saves.
        # Judged first, so a policy assigned by hand that the loader would
        # refuse is not written into a file that does not load.
        policy = config_manager._burned_in_text_policy(
            self.burned_in_text, "session.configuration")
        if policy is not None:
            data["burned_in_text"] = policy
        return yaml.dump(data, sort_keys=False, default_flow_style=False,
                         width=float("inf"))

    def _phi_tags_over(self, base: Dict[str, Any]) -> Dict[str, Any]:
        """The rules of `phi_tags` a file naming `base` must carry.

        The one diff both writers use: `save()` and `Session.create_config()`
        (#741), so a scaffold and a saved file cannot disagree about what
        to write.

        Args:
            base (Dict[str, Any]): The rules the file's `privacy_profile`
                line brings in on reload (`config_manager._policy_base_rules`,
                or, for the scaffold of the floor, its base profile's).

        Returns:
            Dict[str, Any]: Every rule, keys lowercased, that differs whole
            from `base`'s rule for its tag, or that `base` lacks.

        Raises:
            ValueError: When `phi_tags` lacks a rule `base` supplies,
                because the reload would bring it back.
        """
        # Keys as the loader reads them, lowercase: `phi_tags` assigned in
        # code can hold `0008,103E`, which the base spells `0008,103e`.
        # Compared raw, that rule would read as missing and the save would
        # refuse a policy that has it.
        tags = config_manager._lowercase_tag_keys(self.phi_tags)

        missing = [tag for tag in base if tag not in tags]
        if missing:
            raise ValueError(self._missing_base_rules_refusal(missing))

        # Whole rules, not actions: an action-only diff (the scaffold's
        # until #741) drops a rule that keeps the base's action and adds a
        # `value` or a `name`.
        return {tag: rule for tag, rule in tags.items() if base.get(tag) != rule}

    def _missing_base_rules_refusal(self, missing: List[str]) -> str:
        """Why `save()` cannot write a policy that lacks rules its base
        supplies.

        Names up to three missing tags and how to opt one out; for an
        external profile, also suggests reloading it, since the profile
        file may have gained the rules after the load.

        Args:
            missing (List[str]): The tags the base supplies and `phi_tags`
                lacks.

        Returns:
            str: The refusal message.
        """
        # Two ways here: a rule deleted from `phi_tags` directly (no method
        # removes one), or, for an external profile only, a rule the profile
        # file gained after the load. Without a snapshot the save cannot
        # tell them apart, so the message names both where both are possible.
        if self.privacy_profile:
            base = f"privacy_profile {self.privacy_profile}"
            brings = f"a file naming {self.privacy_profile} brings them in"
        else:
            base = "the floor policy (no privacy_profile line)"
            brings = "a file with no privacy_profile line brings them in"
        shown = ", ".join(missing[:3])
        more = f" (and {len(missing) - 3} more)" if len(missing) > 3 else ""
        message = (f"configuration.save() cannot write this policy over {base}: "
                   f"phi_tags has no rule for {shown}{more}, and {brings}. If you "
                   f"deleted them, give each a rule instead, e.g. "
                   f"set_phi_tag({missing[0]!r}, 'KEEP'), which is how a file "
                   f"opts one tag out")
        pinned = profiles.PROFILE_ALIASES.get(self.privacy_profile, self.privacy_profile)
        if self.privacy_profile and pinned not in profiles.PRIVACY_PROFILES:
            message += ("; if the profile file has changed since load_config(), "
                        "load it again")
        return message

    def _refuse_auto_save_without_a_file(self) -> None:
        """Refuse an opted-in configuration with nowhere to write.

        Called first in every mutator, before anything changes. About the
        setting, not the call, so a `delete_rule` that would change nothing
        refuses too.

        Raises:
            ValueError: When `auto_save` is on and `config_path` is unset.
        """
        if self.auto_save and not self.config_path:
            raise ValueError(_NO_FILE)

    def _apply(self, change: Callable[["IsocenterConfiguration"], Any]) -> Any:
        """Make `change` to this configuration, and write it when
        `auto_save` is on.

        Under auto-save a refused or failed write leaves memory and the
        file exactly as they were. With auto-save off, the change stays in
        memory, and the first one after a load or a save prints a one-line
        notice that the file is unchanged.

        Args:
            change (Callable[[IsocenterConfiguration], Any]): Applies the
                change to the configuration it is given. Must be
                deterministic: under auto-save it runs twice.

        Returns:
            Any: What `change` returned for this object.

        Raises:
            ValueError: Under `auto_save`, with no `config_path` or when
                `save()` refuses.
            OSError: Under `auto_save`, when the write fails.
        """
        # The four mutators all come through here, after their validation,
        # so they cannot drift apart. Under auto-save the change is tried
        # first on a deep copy and that copy is saved; only a save that
        # succeeded lets the change reach this object. Nothing needs
        # restoring, and a rule `get_rule()` handed out stays the
        # configuration's own dict, which a snapshot-and-restore would
        # replace.
        self._refuse_auto_save_without_a_file()
        if self.auto_save:
            trial = copy.deepcopy(self)
            change(trial)
            trial.save()
            result = change(self)
            self._file_in_sync = True
            return result
        result = change(self)
        if self.config_path and self._file_in_sync:
            self._file_in_sync = False
            print(f"Configuration changed in memory only; {self.config_path} is "
                  f"unchanged. Call session.configuration.save() to write it, or "
                  f"set session.configuration.auto_save = True.")
        return result

    @staticmethod
    def _without_rule(configuration: "IsocenterConfiguration", serial_number: str) -> bool:
        """Remove `serial_number`'s rule from `configuration`.

        Args:
            configuration (IsocenterConfiguration): The configuration to
                change.
            serial_number (str): The serial whose rules are removed.

        Returns:
            bool: Whether a rule was removed.
        """
        # A `match:` rule has no serial and is never removed by one (#1108).
        before = len(configuration.rules)
        configuration.rules = [r for r in configuration.rules
                               if "match" in r
                               or r.get("serial_number") != serial_number]
        return len(configuration.rules) < before

    def add_rule(self, serial_number: str, manufacturer: str = "Unknown",
                 model_name: str = "Unknown",
                 redaction_zones: List[Any] = None) -> None:
        """
        Add a machine redaction rule.

        It replaces any existing rule for the same serial number. It
        changes memory, and it writes `config_path` only when `auto_save`
        is on. The keywords have the same spelling as the rule's keys in a
        `machines:` file.

        Args:
            serial_number (str): The device serial number.
            manufacturer (str, optional): Metadata for reference.
            model_name (str, optional): Metadata for reference.
            redaction_zones (List[Any], optional): The zones to set to zero.
                Each zone is `[y1, y2, x1, x2]` in pixels: rows `y1` up to
                `y2` and columns `x1` up to `x2`, with the end excluded. A
                zone with no area (`y2 <= y1` or `x2 <= x1`) makes
                `redact()` fail that instance. A zone that starts past the
                image's edge is skipped.

        Raises:
            ValueError: For a rule that `load_config` would refuse (a
                serial that is not a non-empty, non-blank string, a
                metadata field that is not a string, a malformed zone),
                before any rule or the file changes. Also under
                `auto_save`, if there is no `config_path` or if `save()`
                refuses. The rules are then as they were.
            OSError: Under `auto_save`, when the write fails. The rules
                are then as they were.
        """
        new_rule = {
            "serial_number": serial_number,
            "manufacturer": manufacturer,
            "model_name": model_name,
            "redaction_zones": redaction_zones or []
        }
        # Before the delete, not merely before the append: a refusal after
        # it would lose the serial's existing rule. The loader's own
        # check, so this cannot store a rule the session's
        # next `load_config` of the saved file refuses. Ahead of `_apply`,
        # so a refused rule never reaches the write.
        config_manager.ConfigLoader._validate_rule(new_rule, len(self.rules))

        def change(configuration):
            self._without_rule(configuration, serial_number)
            configuration.rules.append(new_rule)
        self._apply(change)

    def update_rule(self, serial_number: str, updates: Dict[str, Any]) -> None:
        """
        Update the rule for `serial_number`.

        It changes memory, and it writes `config_path` only when
        `auto_save` is on.

        Args:
            serial_number (str): The target rule's serial number.
            updates (Dict[str, Any]): Dictionary of fields to update.

        Raises:
            ValueError: If no rule is found, if the update changes the
                serial number, or if `load_config` would refuse the
                updated rule (an unknown key such as `redaction_zone`, a
                value of the wrong type, a malformed zone). It is raised
                before the rule or the file changes. Also under
                `auto_save`, if there is no `config_path` or if `save()`
                refuses. The rule is then as it was.
            OSError: Under `auto_save`, when the write fails. The rule is
                then as it was.
        """
        rule = self.get_rule(serial_number)
        if not rule:
            raise ValueError(f"No rule found for serial number '{serial_number}'")

        # Prevent changing the serial number via update to avoid identity mismatch logic
        if "serial_number" in updates and updates["serial_number"] != serial_number:
            raise ValueError("Values for 'serial_number' cannot be changed via update_rule.")

        # The rule as it would be, judged before the in-place update, so a
        # typo such as `{"redaction_zone": ...}` is refused rather than
        # stored and saved into a file the loader refuses. Updated in
        # place afterwards, not replaced, because `get_rule` hands out the
        # dict itself -- which is why the change looks the rule up in the
        # configuration it is given rather than closing over `rule`.
        config_manager.ConfigLoader._validate_rule(
            {**rule, **updates}, self.rules.index(rule))
        self._apply(lambda configuration: configuration.get_rule(serial_number).update(updates))

    def delete_rule(self, serial_number: str) -> bool:
        """
        Remove the rule for a serial number.

        It changes memory. It writes `config_path` only when `auto_save`
        is on and a rule was removed.

        Args:
            serial_number (str): The serial number to remove.

        Returns:
            bool: True if a rule was found and removed, False otherwise.

        Raises:
            ValueError: Under `auto_save` with no `config_path` (even
                when no rule matches), and when `save()` refuses.
            OSError: Under `auto_save`, when the write fails. The rules
                are then as they were.
        """
        self._refuse_auto_save_without_a_file()
        if self.get_rule(serial_number) is None:
            return False
        return self._apply(lambda configuration: self._without_rule(configuration, serial_number))

    def set_phi_tag(self, tag: str, action: str, value: str = None,
                    target: Sequence[str] = None) -> None:
        """
        Set or replace the PHI rule for one tag.

        It changes memory, and it writes `config_path` only when
        `auto_save` is on. A private-creator key can have twins: other keys
        of the policy that name its element under another case or spacing
        of its creator. This method sets the same rule on every twin too,
        because all of these keys name one element (#1147).

        Args:
            tag (str): The DICOM tag to target (e.g. "0010,0010"), stored
                lowercase. A private creator's name keeps its case, and
                its surrounding spaces are stripped.
            action (str): One of `KEEP`, `REMOVE`, `EMPTY`, `REPLACE`,
                `SHIFT`, `JITTER` (`JITTER` is `SHIFT`) or `SCRUB`.
            value (str, optional): The value that `REPLACE` writes. It is
                stored as the rule's `value`, the key that a file spells it
                with. An empty value is no value, on every tag. `value=""`
                stores no `value` key, so on a UI tag the rule writes the
                keyed replacement UID. But a file's `value: ''` on a UI tag
                is refused (#883).
            target (Sequence[str], optional): The kinds that `SCRUB`
                removes. It is stored as the rule's `target`, the key that a
                file spells it with. It is a list or tuple of
                `config_manager.SCRUB_KINDS`, kept in the order given. That
                order decides how a span that two kinds find is counted
                (#1130). Only `SCRUB` takes it, and `SCRUB` requires it.

        Raises:
            ValueError: For an unknown action, and for a rule the pipeline
                cannot honour: a Patient ID rule other than KEEP or REPLACE
                with no value, a Study or Series Instance UID rule other
                than KEEP or REPLACE with no value (#877), a `value`
                under an action other than REPLACE, SHIFT/JITTER on a
                standard tag that is not DA or DT, REPLACE on a standard
                tag whose VR cannot hold the value, `SCRUB` with no
                `target` or on a tag that does not hold text, and a
                `target` that is not a list of distinct kinds or is given
                with another action. It is raised before the policy or its
                file changes. Under `auto_save`, it is also raised if there
                is no `config_path` or if `save()` refuses. The policy is
                then as it was.
            OSError: Under `auto_save`, when the write fails. The policy
                is then as it was.
        """
        # One spelling, as every other key in the policy has: lowercase,
        # with a private creator's case kept (#1132; profiles.py's header
        # comment gives the reason). Any other case would store a second
        # rule for a tag the floor already holds, one of which
        # `PhiInspector._normalize_tag_keys` drops at scan time by dict
        # order.
        tag = config_manager._canonical_rule_key(tag)
        # `config_manager` accepts a tag value in either of two shapes --
        # a bare name string, or the structured
        # `{"name": ..., "action": ...}` form -- and this method always
        # writes the structured one, because it is the only shape that
        # can carry the action and the replacement.
        val = {
            "name": "Custom Tag",  # We might not know the name easily without lookup
            "action": action
        }
        # Truthiness on purpose: `""` is no value, so it stores no key and
        # never meets the loader's #883 refusal of `value: ''` on a UI
        # (owner ruling Q3 A).
        if value:
            val["value"] = value
        # A list or tuple is stored as a list of its own, never the caller's
        # list, so a later change to it does not change the policy; anything
        # else is left for the loader's refusal, so a bare string is refused
        # rather than iterated into letters.
        if target is not None:
            val["target"] = (list(target) if isinstance(target, (list, tuple))
                             else target)
        elif str(action).strip().upper() == "SCRUB":
            # The loader's no-target refusal, naming this method's keyword.
            raise ValueError(
                f"set_phi_tag: phi_tags[{tag!r}] is SCRUB with no target; "
                f"pass target=, a list of "
                f"{', '.join(config_manager.SCRUB_KINDS)} "
                f'(set_phi_tag(tag, "SCRUB", target=["dates", "ids"]))')

        # Before the assignment and any write: a refused rule leaves the
        # policy and its file as they were. This refuses an unknown action
        # too (`OBLITERATE`), with the loader's words, rather than storing
        # it to be scanned as REPLACE.
        config_manager.validate_phi_policy({tag: val}, "set_phi_tag")
        # A creator key names one element whatever its creator's case and
        # spacing (#1147), so the policy's other spellings of it (twins)
        # take the same rule: setting one twin alone would leave two keys
        # that act differently on one element, which every door refuses
        # (`_creator_twins_refused`) at the next `audit()`.
        twins = (config_manager._creator_twins(self.phi_tags or {}, tag)
                 if config_manager._is_creator_key(tag) else [])

        def change(configuration):
            for key in twins:
                configuration.phi_tags[key] = copy.deepcopy(val)
            configuration.phi_tags[tag] = val
        self._apply(change)

    def get_rule(self, serial_number: str) -> Optional[Dict[str, Any]]:
        """
        Return the rule for a serial number: the rule dictionary itself,
        not a copy.

        It compares the spelling exactly, and it returns the first match.
        Redaction does not use this method: `redact()` and the export apply
        every matching rule, `"*"` included. A `match:` rule (#1108) has no
        serial, and this method does not return one, and `update_rule` and
        `delete_rule` do not see one either. Write a `match:` rule in the
        file, or append it to `rules`.

        Args:
            serial_number (str): The serial number to find. `""` finds the
                rule for series with no Device Serial Number.

        Returns:
            Optional[Dict[str, Any]]: The rule dictionary if found, else None.
        """
        for r in self.rules:
            if "match" not in r and r.get("serial_number") == serial_number:
                return r
        return None

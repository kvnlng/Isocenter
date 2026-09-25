"""
Defines the runtime configuration structures for the Isocenter application.

This module contains the `IsocenterConfiguration` dataclass which encapsulates everything
needed to drive a session's behavior, including redaction rules, PHI profiling,
and date shifting parameters. It also handles the persistent state of these
settings in a backing YAML file, which it writes only when asked:
`save()`, or every change once `auto_save` is on (#715).
"""
import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional
import yaml

from . import config_manager, profiles
from .entities import ScanPolicy
from .profiles import FLOOR_POLICY

#: `save()` with nowhere to write, and any change under `auto_save` with
#: nowhere to write (#715). A silent return here was the #234 shape: a
#: tier-1 method reporting success after doing nothing.
_NO_FILE = ("configuration.save() has no file to write: load_config(path) "
            "sets one, or set session.configuration.config_path (#715)")


class FlowList(list):
    """A list YAML should render inline, as [a, b, c].

    Redaction zones read as coordinates, not as a bulleted list four
    lines tall. Defined once here and registered once at import: this
    class and its representer previously existed twice, and
    `create_config` re-registered the representer -- mutating global PyYAML
    state -- on every call.
    """


def _flow_list_representer(dumper, data):
    return dumper.represent_sequence(
        'tag:yaml.org,2002:seq', data, flow_style=True)


yaml.add_representer(FlowList, _flow_list_representer)


# --- The policy a PHI status is recorded under (#555) ----------------------
#
# **A store format.** Every status a 1.x store holds carries a `"v1:"`
# fingerprint made here, and a 1.0 store opens in every 1.x, so the v1
# canonical form is never changed: a later 1.x adds `_canonical_policy_v2`
# with a `"v2:"` prefix beside it, and compares a stored v1 record against
# the in-force policy's *v1* fingerprint. A stored fingerprint is never
# rewritten -- the input it hashed is gone, and deriving it again would be
# the back-fill the migration refuses. `test_the_v1_fingerprint_is_pinned`
# holds the bytes, under a fixed `CONFIG_VERSION`: a minor bump moves
# every fingerprint by design and is not a change of the form.
#
# **The rule it keeps: it may tell apart two policies that scan alike, and
# must never equate two that scan differently.** Telling them apart costs a
# re-audit; equating them is #555 again. So nothing is normalized. The
# inspector does not read a rule one way: `_owned_rule` reads
# `rule.get("action") or "REPLACE"` and the configured-tag scan
# `rule.get("action", "REPLACE")`, so `action: ""` is two different things
# at two sites and hashes as itself.
#
# In: every rule key except `name`, the rule's form,
# `remove_private_tags` (on CT_small the difference between 183 findings
# and 4), and `config_manager.CONFIG_VERSION` (#762). Out: `name` and a
# bare-string rule's text, which only label a finding; `date_jitter`,
# which moves a shift and not what is flagged; the pixel rules; the
# project secret; the library version; and the base, which is a label.
#
# **What `CONFIG_VERSION` is doing here (#762, owner's ruling).** The dict
# is not the whole of what a scan does with it: across #760 a value-less
# REPLACE on a UN tag began writing the VR dummy (#556) and a repeating-
# group mask key began resolving (#557), and two identical dicts scanned
# differently under one fingerprint -- the rule above, broken by the
# library rather than the config. A release that changes what an
# unchanged configuration does bumps `CONFIG_VERSION`'s minor, so the
# version names the behaviour the dict was read with, and a store's
# statuses from before the bump read as another policy at export. Not the
# library version: every release would then cost every store a re-audit,
# for releases that change nothing a scan reads. The cost of the version
# is the same in kind and rarer: a minor bumped for a key added, with no
# behaviour changed, tells apart two policies that scan alike. Added
# before any release carried a v1 record, so nothing was migrated, and
# the prefix stayed `v1:`: no store holds a v1 hash of the older form.


def _tagged(value):
    """A value JSON cannot hold, as text that says what it was.

    `_scan_policy()` runs at export with no validator in front of it, over
    a `phi_tags` code can assign, so the canonical form must never raise:
    a rule value YAML read as a `date` hashes as that date, not as a
    string that happens to spell it.
    """
    return {"__type__": type(value).__name__, "__str__": str(value)}


def _key(key):
    # `json.dumps(default=)` never reaches keys, and `sort_keys` raises on
    # keys of mixed types, so a non-str key is replaced by its repr.
    return key if isinstance(key, str) else repr(key)


def _canonical_policy_v1(phi_tags, remove_private_tags) -> bytes:
    """The v1 canonical form of a tag policy. Never change it (see above)."""
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
    # import (CLAUDE.md): the bump is what moves every fingerprint (#762).
    doc = {"config_version": config_manager.CONFIG_VERSION,
           "phi_tags": rules, "remove_private_tags": bool(remove_private_tags)}
    return json.dumps(doc, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, default=_tagged).encode("ascii")


def _scan_policy_for(phi_tags, remove_private_tags, base: str) -> ScanPolicy:
    """The `ScanPolicy` a scan over `phi_tags` records, labelled `base`."""
    return ScanPolicy("v1:" + hashlib.sha256(
        _canonical_policy_v1(phi_tags, remove_private_tags)).hexdigest(), base)


def _policy_base_label(base) -> str:
    """The loader's fifth element as the label a person reads (#714, #555).

    `profiles.FLOOR` is the floor, None is `privacy_profile: none`, and a
    string is a pinned profile name or an external profile's path. The one
    spelling of each, shared by `IsocenterConfiguration._policy_base` and
    `Session.audit(config_path=)`, so one base cannot be written two ways.
    """
    if base is profiles.FLOOR:
        return f"floor over {profiles.FLOOR_BASE}"
    if base is None:
        return "none"
    return base


#: What `(0012,0063)` calls a policy whose base is an external profile's
#: path (#554): the path is the operator's directory layout, and exported
#: data is de-identification scope (#655).
EXTERNAL_PROFILE_LABEL = "external profile"


def _deid_method_label(base: str) -> str:
    """A recorded `ScanPolicy.base` as De-identification Method names it
    (#554): verbatim when it is one of the shapes this library spells --
    a pinned profile name, `none`, or the floor's label -- and
    `external profile` for anything else, which is a path.

    Exact matches only, so no string that merely looks like one of them
    (a pinned name in another case, a relative path beginning `floor
    over `) is written verbatim.
    """
    if (base in profiles.PRIVACY_PROFILES
            or base in (_policy_base_label(None),
                        _policy_base_label(profiles.FLOOR))):
        return base
    return EXTERNAL_PROFILE_LABEL


def _deid_method_value(policy: ScanPolicy, version: str) -> str:
    """This step's De-identification Method `(0012,0063)` value (#554,
    owner ruling Q4): `isocenter/<version>; <label>; v1:<8 hex>`.

    - `isocenter/<version>` is the exact spelling the output fingerprint's
      N2 substitution normalises (`scripts/output_fingerprint.py`), so a
      release bump moves no recorded output. Any other spelling would.
    - The label is the recorded policy's, through `_deid_method_label`.
    - 8 hex characters of `ScanPolicy.fingerprint`, never recomputed, so
      there is one answer to "which policy". It carries `CONFIG_VERSION`
      (#762), so a minor bump moves it in every exported file. Eight,
      not more, for LO's 64: the floor's label with a 17-character
      version is exactly 64 (`tests/test_an_export_says_how_it_was_de_
      identified.py`, M12).
    """
    scheme, _, digest = policy.fingerprint.partition(":")
    return (f"isocenter/{version}; {_deid_method_label(policy.base)}; "
            f"{scheme}:{digest[:8]}")


@dataclass
class IsocenterConfiguration:
    """
    Encapsulates the runtime configuration for a DicomSession.

    Attributes:
        rules (List[Dict[str, Any]]): List of machine redaction rules.
        phi_tags (Dict[str, Any]): PHI tag policies (e.g. {tag: action}).
            With none given, a copy of `profiles.FLOOR_POLICY`, the
            policy a session applies before any config is loaded. Each
            instance holds its own copy, so one session's `set_phi_tag`
            cannot reach another's policy or the module table.
        date_jitter (Dict[str, int]): Date shifting parameters.
        remove_private_tags (bool): Global flag to strip private tags.
        config_path (Optional[str]): The file `save()` writes. Set by
            `load_config()`, or by hand.
        auto_save (bool): Write `config_path` after every `add_rule`,
            `update_rule`, `delete_rule` and `set_phi_tag`. False by
            default.
        privacy_profile (Optional[str]): The pinned name of the built-in
            profile whose rules were merged into `phi_tags` (`basic@2026c`,
            also when the file said `basic`), or an external profile's
            path, or None when no named profile was applied (the floor, or
            `privacy_profile: none`). Only ever set to a profile that
            resolved.
    """
    rules: List[Dict[str, Any]] = field(default_factory=list)
    phi_tags: Dict[str, Any] = field(
        default_factory=lambda: copy.deepcopy(FLOOR_POLICY))
    date_jitter: Dict[str, int] = field(default_factory=lambda: {"min_days": -365, "max_days": -1})
    remove_private_tags: bool = True
    config_path: Optional[str] = None
    privacy_profile: Optional[str] = None
    #: Off by default since 1.0 (#715): a loaded file is the user's, and
    #: one `add_rule()` rewrote a 7-line commented file as 1,872 lines.
    #: Survives `load_config()`, which never assigns it: it is the
    #: session's choice, not the file's, and a later load is written to.
    auto_save: bool = False
    # Whether `phi_tags` came from the floor policy: a bare configuration,
    # or a loaded file with no `privacy_profile` line. `Session.load_config`
    # sets it on every load. The floor and `privacy_profile: none` both
    # leave `privacy_profile` at None, and nothing else here tells them
    # apart -- the report called both "session defaults" until #714.
    # Private and not a constructor parameter, so the frozen field list is
    # unchanged.
    _floor: bool = field(default=True, init=False, repr=False, compare=False)
    # Whether `config_path` holds what memory holds, as far as this object
    # knows: cleared by the first change that stays in memory, set by a
    # save that succeeded and by `Session.load_config`. It exists for the
    # one-line notice (owner ruling Q4), so a 0.9.x script that relied on
    # auto-save is told once that its file stopped following its edits.
    _file_in_sync: bool = field(default=True, init=False, repr=False, compare=False)

    @property
    def _policy_base(self) -> str:
        """What the policy in force was built on, as one string (#714):
        the pinned profile name or external path, `floor over
        basic@2026c`, or `none`. The identifier the report prints and the
        store's policy record (#555) is to carry."""
        if self.privacy_profile:
            return self.privacy_profile
        return _policy_base_label(profiles.FLOOR if self._floor else None)

    def _scan_policy(self) -> ScanPolicy:
        """The policy in force: what `audit()` with no argument scans with
        (#555). Computed on every call, never cached: `phi_tags` and
        `remove_private_tags` can be assigned directly."""
        return _scan_policy_for(self.phi_tags, self.remove_private_tags,
                                self._policy_base)

    def save(self) -> None:
        """
        Write the configuration to `config_path` as YAML.

        The file names the profile rather than copying it:
        `privacy_profile` is the pinned name (`basic@2026c`), the external
        profile's path, `none`, or no line at all for the floor, and
        `phi_tags` holds only the rules that differ from that base's. Then
        `date_jitter`, `remove_private_tags` and every machine rule, each
        key kept (`comment:` as data). `version` is always written, as this
        library's configuration version. A file this writes loads to the
        configuration it was written from.

        Comments and layout in an existing file are not kept: this writes a
        new file.

        Raises:
            ValueError: With no `config_path`; and when `phi_tags` has no
                rule for a tag its base supplies, because a file naming
                that base would bring the rule back on reload. Nothing is
                written.
            OSError: The write's own error, unchanged.
        """
        if not self.config_path:
            raise ValueError(_NO_FILE)
        # Rendered before the file is opened, so a refusal or a rendering
        # failure cannot leave it truncated. Then written in place rather
        # than through a temporary file and `os.replace`, which would turn
        # a symlinked config into a regular file and drop its mode.
        text = self._rendered()
        with open(self.config_path, "w", encoding="utf-8") as handle:
            handle.write(text)
        self._file_in_sync = True

    def _rendered(self) -> str:
        """The YAML `save()` writes, or the `ValueError` it raises."""
        # The base lookup sits beside the loader's resolution, so the two
        # cannot resolve a name differently.
        base = config_manager._policy_base_rules(self.privacy_profile, self._floor)
        # Keys as the loader reads them, lowercase: `phi_tags` assigned in
        # code can hold `0008,103E`, which the base spells `0008,103e`.
        # Compared raw, that rule read as missing and the save refused a
        # policy that has it (review of #742, finding 5).
        tags = config_manager._lowercase_tag_keys(self.phi_tags)

        missing = [tag for tag in base if tag not in tags]
        if missing:
            raise ValueError(self._missing_base_rules_refusal(missing))

        # Whole rules, not actions: an action-only diff (the scaffold's)
        # drops a rule that keeps the base's action and adds a `value`.
        overrides = {tag: rule for tag, rule in tags.items()
                     if base.get(tag) != rule}

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
            # number both writers stamp (#711).
            "version": config_manager.CONFIG_VERSION,
        }
        # No line for the floor: an absent line means the floor in every
        # 1.x (#495, #714). 0.9.8 wrote `none` plus the 620 floor rules,
        # a file that said "exactly these rules" where the session had
        # said "the floor".
        if self.privacy_profile:
            data["privacy_profile"] = self.privacy_profile
        elif not self._floor:
            data["privacy_profile"] = "none"
        data["phi_tags"] = overrides
        data["date_jitter"] = self.date_jitter
        data["remove_private_tags"] = self.remove_private_tags
        data["machines"] = machines
        return yaml.dump(data, sort_keys=False, default_flow_style=False,
                         width=float("inf"))

    def _missing_base_rules_refusal(self, missing: List[str]) -> str:
        """Why `save()` cannot write a policy that lacks rules its base
        supplies (#715). There are two ways there: a rule deleted from
        `phi_tags` directly (no method removes one), or, for an external
        profile only, a rule the profile file gained after the load. The
        save cannot tell them apart without a snapshot, so it names both
        where both are possible."""
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
        return message + " (#715)"

    def _refuse_auto_save_without_a_file(self) -> None:
        """First in every mutator, before anything changes: an opted-in
        configuration with nowhere to write is a mistake to report, not a
        no-op (#715). About the setting, not the call, so a `delete_rule`
        that would change nothing refuses too."""
        if self.auto_save and not self.config_path:
            raise ValueError(_NO_FILE)

    def _apply(self, change: Callable[["IsocenterConfiguration"], Any]) -> Any:
        """Make `change` to this configuration, and write it when
        `auto_save` is on (#715). The four mutators all come through here,
        after their validation, so they cannot drift apart.

        Under auto-save the change is tried first on a deep copy and that
        copy is saved; only a save that succeeded lets the change reach
        this object. A refused or failed write therefore leaves memory and
        the file exactly as they were, with nothing to restore -- and a
        rule `get_rule()` handed out is still the configuration's own
        dict, which a snapshot-and-restore would have replaced. `change`
        must be deterministic: it runs twice.

        With auto-save off, the change stays in memory, and the first one
        after a load or a save says so, once (owner ruling Q4).
        """
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
                  f"set session.configuration.auto_save = True (#715).")
        return result

    @staticmethod
    def _without_rule(configuration: "IsocenterConfiguration", serial_number: str) -> bool:
        """Remove `serial_number`'s rule from `configuration`; whether one
        was there."""
        before = len(configuration.rules)
        configuration.rules = [r for r in configuration.rules
                               if r.get("serial_number") != serial_number]
        return len(configuration.rules) < before

    def add_rule(self, serial_number: str, manufacturer: str = "Unknown",
                 model_name: str = "Unknown",
                 redaction_zones: List[Any] = None) -> None:
        """
        Add a machine redaction rule.

        Replaces any existing rule for the same serial number. Changes
        memory; writes `config_path` only when `auto_save` is on.

        Args:
            serial_number (str): The device serial number.
            manufacturer (str, optional): Metadata for reference.
            model_name (str, optional): Metadata for reference.
            redaction_zones (List[Any], optional): List of redaction zones
                (ROIs).

        The keywords are spelled as the rule's keys in a `machines:` file.

        Raises:
            ValueError: For a rule `load_config` would refuse (a serial
                that is not a non-empty, non-blank string, a metadata field
                that is not a string, a malformed zone), before any rule or
                the file changes. Under `auto_save`, with no `config_path`,
                or when `save()` refuses; the rules are then as they were.
            OSError: Under `auto_save`, when the write fails; the rules
                are then as they were.
        """
        new_rule = {
            "serial_number": serial_number,
            "manufacturer": manufacturer,
            "model_name": model_name,
            "redaction_zones": redaction_zones or []
        }
        # Before the delete, not merely before the append: a refusal after
        # it would have lost the serial's existing rule (#712). The
        # loader's own check, so this cannot store a rule the session's
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

        Changes memory; writes `config_path` only when `auto_save` is on.

        Args:
            serial_number (str): The target rule's serial number.
            updates (Dict[str, Any]): Dictionary of fields to update.

        Raises:
            ValueError: If no rule is found, if the update changes the
                serial number, or if the updated rule is one `load_config`
                would refuse (an unknown key such as `redaction_zone`, a
                value of the wrong type, a malformed zone). Raised before
                the rule or the file changes. Under `auto_save`, with no
                `config_path`, or when `save()` refuses; the rule is then
                as it was.
            OSError: Under `auto_save`, when the write fails; the rule is
                then as it was.
        """
        rule = self.get_rule(serial_number)
        if not rule:
            raise ValueError(f"No rule found for serial number '{serial_number}'")

        # Prevent changing the serial number via update to avoid identity mismatch logic
        if "serial_number" in updates and updates["serial_number"] != serial_number:
            raise ValueError("Values for 'serial_number' cannot be changed via update_rule.")

        # The rule as it would be, judged before the in-place update: the
        # typo `{"redaction_zone": ...}` was stored and auto-saved, writing
        # a file this session's own loader then refused (#712). Updated in
        # place afterwards, not replaced, because `get_rule` hands out the
        # dict itself -- which is why the change looks the rule up in the
        # configuration it is given rather than closing over `rule`.
        config_manager.ConfigLoader._validate_rule(
            {**rule, **updates}, self.rules.index(rule))
        self._apply(lambda configuration: configuration.get_rule(serial_number).update(updates))

    def delete_rule(self, serial_number: str) -> bool:
        """
        Remove the rule for a serial number.

        Changes memory; writes `config_path` only when `auto_save` is on
        and a rule was removed.

        Args:
            serial_number (str): The serial number to remove.

        Returns:
            bool: True if a rule was found and removed, False otherwise.

        Raises:
            ValueError: Under `auto_save` with no `config_path` (even
                when no rule matches), and when `save()` refuses.
            OSError: Under `auto_save`, when the write fails; the rules
                are then as they were.
        """
        self._refuse_auto_save_without_a_file()
        if self.get_rule(serial_number) is None:
            return False
        return self._apply(lambda configuration: self._without_rule(configuration, serial_number))

    def set_phi_tag(self, tag: str, action: str, value: str = None) -> None:
        """
        Set or replace the PHI rule for one tag.

        Changes memory; writes `config_path` only when `auto_save` is on.

        Args:
            tag (str): The DICOM tag to target (e.g. "0010,0010"), stored
                lowercase.
            action (str): The remediation action ('KEEP', 'REMOVE', 'REPLACE', 'JITTER', 'EMPTY').
            value (str, optional): The value `REPLACE` writes, stored as
                the rule's `value`, the key a file spells it with.

        Raises:
            ValueError: For an unknown action, and for a rule the pipeline
                cannot honour: a Patient ID rule other than KEEP or REPLACE
                with no value, a `value` under an action other than
                REPLACE, SHIFT/JITTER on a standard tag that is not DA or
                DT, or REPLACE on a standard tag whose VR cannot hold the
                value. Raised before the policy or its file is changed.
                Under `auto_save`, also with no `config_path` and when
                `save()` refuses; the policy is then as it was.
            OSError: Under `auto_save`, when the write fails; the policy
                is then as it was.
        """
        # Lowercase, as every other key in the policy is (profiles.py's
        # header comment gives the reason). This was `tag.upper()`, so
        # `set_phi_tag("0008,103e", ...)` stored `0008,103E` beside the
        # floor's own `0008,103e`: two rules for one tag, one of which
        # `PhiInspector._normalize_tag_keys` silently dropped at scan time
        # by dict order, and a report counting both (#495).
        tag = tag.lower()
        # `config_manager` accepts a tag value in either of two shapes --
        # a bare name string, or the structured
        # `{"name": ..., "action": ...}` form -- and this method always
        # writes the structured one, because it is the only shape that
        # can carry the action and the replacement.
        #
        # The line number that used to be cited here for that fact
        # pointed past the end of `config_manager.py` and what it
        # originally referred to is unrecoverable, so it is deleted
        # rather than renumbered to a guess (#310). The claim itself is
        # carried by the structured-tag tests, not by prose.
        val = {
            "name": "Custom Tag",  # We might not know the name easily without lookup
            "action": action
        }
        if value:
            val["value"] = value

        # Before the assignment and any write (#456): a refused rule leaves
        # the policy and its file as they were. This refuses an unknown
        # action too, with the loader's words; until 0.9.8 `OBLITERATE`
        # was stored and scanned as REPLACE.
        config_manager.validate_phi_policy({tag: val}, "set_phi_tag")

        def change(configuration):
            configuration.phi_tags[tag] = val
        self._apply(change)

    def get_rule(self, serial_number: str) -> Optional[Dict[str, Any]]:
        """
        Return the rule for a serial number: the rule dictionary itself,
        not a copy.

        Exact spelling, first match. Redaction does not use this: `redact()`
        and the export apply every matching rule, `"*"` included.

        Args:
            serial_number (str): The serial number to find.

        Returns:
            Optional[Dict[str, Any]]: The rule dictionary if found, else None.
        """
        for r in self.rules:
            if r.get("serial_number") == serial_number:
                return r
        return None

"""
Defines the runtime configuration structures for the Isocenter application.

This module contains the `IsocenterConfiguration` dataclass which encapsulates everything
needed to drive a session's behavior, including redaction rules, PHI profiling,
and date shifting parameters. It also handles the persistent state of these
settings in a backing YAML file, which it writes only when asked:
`save()`, or every change once `auto_save` is on (#715).
"""
import copy
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional
import yaml

from . import config_manager, profiles
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


@dataclass
class IsocenterConfiguration:
    """
    Encapsulates the runtime configuration for a DicomSession.

    Attributes:
        rules (List[Dict[str, Any]]): List of machine redaction rules.
        phi_tags (Dict[str, Any]): PHI tag policies (e.g. {tag: action}).
            With none given, a copy of `profiles.FLOOR_POLICY` -- the
            policy a session applies before any config is loaded (#495).
            A copy per instance, so one session's `set_phi_tag` cannot
            reach another's policy or the module table.
        date_jitter (Dict[str, int]): Date shifting parameters.
        remove_private_tags (bool): Global flag to strip private tags.
        config_path (Optional[str]): The file `save()` writes. Set by
            `load_config()`, or by hand.
        auto_save (bool): Write `config_path` after every `add_rule`,
            `update_rule`, `delete_rule` and `set_phi_tag`. False by
            default (#715).
        privacy_profile (Optional[str]): The pinned name of the built-in
            profile whose rules were merged into `phi_tags` -- `basic@2026c`,
            also when the file said `basic` (#714) -- or an external
            profile's path, or None when no named profile was applied (the
            floor, or `privacy_profile: none`). Only ever set to a profile
            that actually resolved, so the compliance report cannot name
            protection that never ran.
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
        if self._floor:
            return f"floor over {profiles.FLOOR_BASE}"
        return "none"

    def save(self) -> None:
        """
        Writes the configuration to `config_path` as YAML (#715).

        The file names the profile rather than copying it:
        `privacy_profile` is the pinned name (`basic@2026c`), the external
        profile's path, `none`, or no line at all for the floor, and
        `phi_tags` holds only the rules that differ from that base's. Then
        `date_jitter`, `remove_private_tags` and every machine rule, each
        key kept (`comment:` as data). `version` is always written, as
        `config_manager.CONFIG_VERSION` (owner ruling Q5): the loader
        accepts only what this library reads, so what is written is that
        version's content. A file this writes loads to the configuration it
        was written from.

        Comments and layout in the file are not kept: this writes a new
        file. A comment-keeping writer (`ruamel.yaml`) was measured and
        rejected -- it moved a deleted rule's comment onto the next rule,
        and it reads YAML 1.2, where `no` and `0123` differ from the
        loader's readings.

        Raises:
            ValueError: With no `config_path`; and when `phi_tags` has no
                rule for a tag its base supplies, because a file naming
                that base would bring the rule back on reload. Nothing is
                written.
            OSError: The write's own error, unchanged. Until 1.0 it was
                printed as a WARNING and the call returned.
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

        missing = [tag for tag in base if tag not in self.phi_tags]
        if missing:
            raise ValueError(self._missing_base_rules_refusal(missing))

        # Whole rules, not actions: an action-only diff (the scaffold's)
        # drops a rule that keeps the base's action and adds a `value`.
        overrides = {tag: rule for tag, rule in self.phi_tags.items()
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
                 model: str = "Unknown", zones: List[Any] = None) -> None:
        """
        Adds a new machine redaction rule.

        Overrides any existing rule for the same serial number. Changes
        memory; writes `config_path` only when `auto_save` is on (#715).

        Args:
            serial_number (str): The device serial number.
            manufacturer (str, optional): Metadata for reference.
            model (str, optional): Metadata for reference.
            zones (List[Any], optional): List of redaction zones (ROIs).

        Raises:
            ValueError: For a rule `load_config` would refuse
                (`ConfigLoader._validate_rule`: a serial that is not a
                non-empty, non-blank string, a metadata field that is not
                a string, a malformed zone), before any rule or the file
                changes. Under `auto_save`, with no `config_path`, or when
                `save()` refuses; the rules are then as they were.
            OSError: Under `auto_save`, when the write fails; the rules
                are then as they were.
        """
        new_rule = {
            "serial_number": serial_number,
            "manufacturer": manufacturer,
            "model_name": model,
            "redaction_zones": zones or []
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
        Updates an existing rule identified by `serial_number`.

        Changes memory; writes `config_path` only when `auto_save` is on
        (#715).

        Args:
            serial_number (str): The target rule's serial number.
            updates (Dict[str, Any]): Dictionary of fields to update.

        Raises:
            ValueError: If rule is not found, if attempting to change the
                serial number, or if the updated rule is one `load_config`
                would refuse (`ConfigLoader._validate_rule`: an unknown key
                such as `redaction_zone`, a value of the wrong type, a
                malformed zone). Raised before the rule or the file
                changes. Under `auto_save`, with no `config_path`, or when
                `save()` refuses; the rule is then as it was.
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
        Removes a rule by serial number.

        Changes memory; writes `config_path` only when `auto_save` is on
        and a rule was removed (#715).

        Args:
            serial_number (str): The serial number to remove.

        Returns:
            bool: True if a rule was found and removed, False otherwise.

        Raises:
            ValueError: Under `auto_save` with no `config_path` -- even
                when no rule matches -- and when `save()` refuses.
            OSError: Under `auto_save`, when the write fails; the rules
                are then as they were.
        """
        self._refuse_auto_save_without_a_file()
        if self.get_rule(serial_number) is None:
            return False
        return self._apply(lambda configuration: self._without_rule(configuration, serial_number))

    def set_phi_tag(self, tag: str, action: str, replacement: str = None) -> None:
        """
        Sets or updates a PHI tag policy.

        Args:
            tag (str): The DICOM tag to target (e.g. "0010,0010").
            action (str): The remediation action ('KEEP', 'REMOVE', 'REPLACE', 'JITTER', 'EMPTY').
            replacement (str, optional): The value `REPLACE` writes, stored
                as the rule's `value` (#538). Until 0.9.8 it was stored
                under a `replacement` key nothing read, and `ANONYMIZED`
                was written.

        Raises:
            ValueError: For an unknown action, and for a rule the pipeline
                cannot honour (`config_manager.validate_phi_policy`: a
                Patient ID rule other than KEEP or REPLACE with no value,
                a `replacement` under an action other than REPLACE,
                SHIFT/JITTER on a standard tag that is not DA or DT, or
                REPLACE on a standard tag whose VR cannot hold the value).
                Raised before the policy or its file is changed. Under
                `auto_save`, also with no `config_path` and when `save()`
                refuses; the policy is then as it was.
            OSError: Under `auto_save`, when the write fails; the policy
                is then as it was.

        Changes memory; writes `config_path` only when `auto_save` is on
        (#715).
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
        if replacement:
            val["value"] = replacement

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
        Retrieves a specific rule dictionary (reference).

        Exact spelling, first match. Redaction does not use this: `redact()`
        and the export apply every matching rule, `"*"` included, through
        `rules_matching` in `services.py` (#580).

        Args:
            serial_number (str): The serial number to find.

        Returns:
            Optional[Dict[str, Any]]: The rule dictionary if found, else None.
        """
        for r in self.rules:
            if r.get("serial_number") == serial_number:
                return r
        return None

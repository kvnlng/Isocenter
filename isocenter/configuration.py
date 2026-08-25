"""
Defines the runtime configuration structures for the Isocenter application.

This module contains the `IsocenterConfiguration` dataclass which encapsulates everything
needed to drive a session's behavior, including redaction rules, PHI profiling,
and date shifting parameters. It also handles the persistent state of these
settings by syncing with a backing YAML file.
"""
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
import yaml


@dataclass
class IsocenterConfiguration:
    """
    Encapsulates the runtime configuration for a DicomSession.

    Attributes:
        rules (List[Dict[str, Any]]): List of machine redaction rules.
        phi_tags (Dict[str, Any]): PHI tag policies (e.g. {tag: action}).
        date_jitter (Dict[str, int]): Date shifting parameters.
        remove_private_tags (bool): Global flag to strip private tags.
        config_path (Optional[str]): Path to the backing YAML file for auto-save.
        privacy_profile (Optional[str]): Name of the profile whose rules were
            merged into `phi_tags`, or None when none was applied. Only ever
            set to a profile that actually resolved -- an unknown reference is
            warned about and dropped at load, so the compliance report cannot
            name protection that never ran.
    """
    rules: List[Dict[str, Any]] = field(default_factory=list)
    phi_tags: Dict[str, Any] = field(default_factory=dict)
    date_jitter: Dict[str, int] = field(default_factory=lambda: {"min_days": -365, "max_days": -1})
    remove_private_tags: bool = True
    config_path: Optional[str] = None
    privacy_profile: Optional[str] = None

    def save(self) -> None:
        """
        Persists the current configuration state to `config_path` (YAML).

        Attempts to format lists as flow-style (bracketed) for better readability.
        """
        if not self.config_path:
            return

        class FlowList(list):
            """Marks lists for flow-style (bracketed) YAML formatting."""

        def flow_list_representer(dumper, data):
            return dumper.represent_sequence('tag:yaml.org,2002:seq', data, flow_style=True)

        try:
            yaml.add_representer(FlowList, flow_list_representer)
        except ValueError:
            # Already registered
            pass

        # Construct Unified Data
        # Re-using logic similar to session.create_config but simplified for direct object dump

        # 4b. Enhance PHI Tags (Transform to structured if needed for saving)
        # We store them as they are set (which should be structured if coming from set_phi_tag)
        # But for cleanliness in YAML, let's ensure consistency.

        # Prepare machines
        machines_export = []
        for m in self.rules:
            m_copy = m.copy()
            if "redaction_zones" in m_copy:
                # Wrap internal lists
                zones = m_copy["redaction_zones"]
                new_zones = FlowList()
                for z in zones:
                    if isinstance(z, list):
                        new_zones.append(FlowList(z))
                    else:
                        new_zones.append(z)
                m_copy["redaction_zones"] = new_zones
            machines_export.append(m_copy)

        data = {
            "version": "2.0",
            # The profile that actually produced these tags, so a round-trip
            # through save() does not relabel a 'basic' config as 'custom'.
            "privacy_profile": self.privacy_profile or "custom",
            "phi_tags": self.phi_tags,
            "date_jitter": self.date_jitter,
            "remove_private_tags": self.remove_private_tags,
            "machines": machines_export
        }

        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                yaml.dump(data, f, sort_keys=False, default_flow_style=False, width=float("inf"))
        except (IOError, OSError, yaml.YAMLError) as e:
            # We don't want to crash the runtime if save fails, but we should log/warn
            # Since we don't have logger here easily without import
            print(f"WARNING: Failed to auto-save configuration: {e}")

    def add_rule(self, serial_number: str, manufacturer: str = "Unknown",
                 model: str = "Unknown", zones: List[Any] = None) -> None:
        """
        Adds a new machine redaction rule.

        Overrides any existing rule for the same serial number. Auto-saves if
        config_path is set.

        Args:
            serial_number (str): The device serial number.
            manufacturer (str, optional): Metadata for reference.
            model (str, optional): Metadata for reference.
            zones (List[Any], optional): List of redaction zones (ROIs).
        """
        # Remove existing if any
        self.delete_rule(serial_number)

        new_rule = {
            "serial_number": serial_number,
            "manufacturer": manufacturer,
            "model_name": model,
            "redaction_zones": zones or []
        }
        self.rules.append(new_rule)
        self.save()

    def update_rule(self, serial_number: str, updates: Dict[str, Any]) -> None:
        """
        Updates an existing rule identified by `serial_number`.

        Args:
            serial_number (str): The target rule's serial number.
            updates (Dict[str, Any]): Dictionary of fields to update.

        Raises:
            ValueError: If rule is not found or if attempting to change the serial number.
        """
        rule = self.get_rule(serial_number)
        if not rule:
            raise ValueError(f"No rule found for serial number '{serial_number}'")

        # Prevent changing the serial number via update to avoid identity mismatch logic
        if "serial_number" in updates and updates["serial_number"] != serial_number:
            raise ValueError("Values for 'serial_number' cannot be changed via update_rule.")

        rule.update(updates)
        self.save()

    def delete_rule(self, serial_number: str) -> bool:
        """
        Removes a rule by serial number.

        Args:
            serial_number (str): The serial number to remove.

        Returns:
            bool: True if a rule was found and removed, False otherwise.
        """
        initial_len = len(self.rules)
        self.rules = [r for r in self.rules if r.get("serial_number") != serial_number]
        removed = len(self.rules) < initial_len
        if removed:
            self.save()
        return removed

    def set_phi_tag(self, tag: str, action: str, replacement: str = None) -> None:
        """
        Sets or updates a PHI tag policy.

        Args:
            tag (str): The DICOM tag to target (e.g. "0010,0010").
            action (str): The remediation action ('KEEP', 'REMOVE', 'REPLACE', 'JITTER', 'EMPTY').
            replacement (str, optional): The replacement value if action is 'REPLACE'.
        """
        tag = tag.upper()
        # Simple string format storage check?
        # config_manager uses dicts or strings. We standardize on dict for API set?
        # Or store as the system expects. System expects:
        # { "0010,0010": "Patient Name" } OR { "0010,0010": {"name": "...", "action": "..."} }

        # If the backend supports structured tags, use that.
        # Based on config_manager.py line 523, it supports structured.

        val = {
            "name": "Custom Tag",  # We might not know the name easily without lookup
            "action": action
        }
        if replacement:
            val["replacement"] = replacement

        self.phi_tags[tag] = val
        self.save()

    def get_rule(self, serial_number: str) -> Optional[Dict[str, Any]]:
        """
        Retrieves a specific rule dictionary (reference).

        Args:
            serial_number (str): The serial number to find.

        Returns:
            Optional[Dict[str, Any]]: The rule dictionary if found, else None.
        """
        for r in self.rules:
            if r.get("serial_number") == serial_number:
                return r
        return None

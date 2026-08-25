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
import json
import re
import yaml

from dotenv import load_dotenv

from .profiles import PRIVACY_PROFILES

CONFIG_VERSION = "2.0"

# Load environment variables
load_dotenv()


def get_logger() -> logging.Logger:
    """
    Retrieves the configured logger for the Isocenter application.

    Returns:
        logging.Logger: The 'isocenter' logger instance.
    """
    return logging.getLogger("isocenter")


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

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    # Handle Standard Config
    config = data

    # Merge Privacy Profile
    if "privacy_profile" in config:
        profile_name = config["privacy_profile"]

        profile_rules = {}

        # 1. Check Built-in Profiles
        if profile_name in PRIVACY_PROFILES:
            profile_rules = copy.deepcopy(PRIVACY_PROFILES[profile_name])
            get_logger().info("Loaded built-in privacy profile '%s' with %d rules.", profile_name, len(profile_rules))

        # 2. Check External File (Custom Profile)
        elif os.path.exists(profile_name):
            try:
                # We reuse load_phi_config logic to parse just the tags
                profile_rules = ConfigLoader.load_phi_config(profile_name)
                get_logger().info("Loaded custom privacy profile from '%s' with %d rules.", profile_name, len(profile_rules))
            except (ValueError, OSError) as e:
                get_logger().error("Failed to load custom profile '%s': %s", profile_name, e)

        else:
            get_logger().warning("Unknown privacy profile reference '%s' (not a built-in or file). Ignoring.", profile_name)

        if not profile_rules:
            # Ignoring it means ignoring it everywhere. Leaving the name in
            # the config would let the compliance report name a profile that
            # contributed no rules -- protection that never ran.
            config.pop("privacy_profile", None)

        if profile_rules:
            # User rules override profile rules
            user_rules = config.get("phi_tags", {})
            profile_rules.update(user_rules)
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

        phi_tags = data.get("phi_tags", {})
        # Support 'machines' (v2) or 'machine_rules' (legacy internal)
        machine_rules = data.get("machines", data.get("machine_rules", []))

        # Date Jitter Normalization
        dj = data.get("date_jitter", {"min_days": -365, "max_days": -1})
        if isinstance(dj, int):
            # Legacy support or user provided int. Convert to fixed shift.
            date_jitter_config = {"min_days": dj, "max_days": dj}
        else:
            date_jitter_config = dj

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
            filepath (str, optional): Path to config file. If None, loads internal defaults.

        Returns:
            Dict: Mapping of tags to configuration (action/name).
        """
        if filepath:
            data = ConfigLoader._load_yaml(filepath)

            # Support v2 unified file used as simple PHI config
            if "phi_tags" in data:
                return data["phi_tags"]
            return data.get("phi_tags", data)  # Fallback to assumes root dict is tags if no key
        else:
            # Load default from package resources
            base = os.path.dirname(os.path.abspath(__file__))
            filepath = os.path.join(base, "resources", "phi_tags.json")
            if os.path.exists(filepath):
                # Read with json, not the YAML helper: user-facing config
                # files are YAML-only by design, but this is a shipped
                # package resource and stays JSON.
                with open(filepath, 'r', encoding="utf-8") as f:
                    return json.load(f).get("phi_tags", {})
            return {}

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

            if not roi or not isinstance(roi, list) or len(roi) != 4:
                raise ValueError(
                    f"Rule #{index} ({sn}), Zone #{z_idx}: ROI must be a list of 4 integers.")

            r1, r2, c1, c2 = roi
            if any(x < 0 for x in roi):
                raise ValueError(
                    f"Rule #{index} ({sn}), Zone #{z_idx}: ROI values must be non-negative.")

            if r1 > r2 or c1 > c2:
                raise ValueError(
                    f"Rule #{index} ({sn}), Zone #{z_idx}: Invalid ROI logic (Start > End).")

"""
Module for analyzing OCR findings and suggesting configuration updates.
"""
from typing import List, Dict, Any
from collections import defaultdict
from isocenter.privacy import PhiReport
from isocenter.configuration import IsocenterConfiguration

class ConfigAutomator:
    """
    Analyzes OCR findings and generates suggestions to update the redaction configuration.
    """

    @staticmethod
    def suggest_config_updates(report: PhiReport, _current_config: IsocenterConfiguration) -> List[Dict[str, Any]]:
        """Generates a list of suggested configuration changes.

        A `NEW_LEAK` finding suggests its text box as a new zone; a
        `PARTIAL_LEAK` finding suggests growing its best-matching zone to
        cover the text. A finding with no `rule_serial` in its metadata
        gets no suggestion. Zones are in config space, (y1, y2, x1, x2),
        the order every consumer of ``redaction_zones`` reads; OCR boxes
        arrive as (x, y, w, h) and are converted.

        Args:
            report (PhiReport): Findings from a pixel scan, each carrying
                `leak_type`, `text_box`, `best_zone` and `rule_serial` in
                its metadata.
            _current_config (IsocenterConfiguration): Unused.

        Returns:
            List[Dict[str, Any]]: One dict per suggestion, with `serial`,
                `action` and `reason`; an `ADD_ZONE` suggestion carries
                `zone`, an `EXPAND_ZONE` one `original_zone` and
                `new_zone`, each `[y1, y2, x1, x2]`.
        """
        suggestions = []

        # Group findings by machine serial
        findings_by_serial = defaultdict(list)

        for finding in report:
            meta = finding.metadata
            if not meta:
                continue

            serial = meta.get("rule_serial")
            if serial:
                findings_by_serial[serial].append(finding)
            else:
                # A finding with no matching rule gets no suggestion.
                pass

        for serial, findings in findings_by_serial.items():
            for f in findings:
                meta = f.metadata
                l_type = meta.get("leak_type")
                text_box = meta.get("text_box") # x,y,w,h

                if not text_box:
                    continue

                if l_type == "PARTIAL_LEAK":
                    # Suggest expanding the best_zone to cover text_box
                    best_zone = meta.get("best_zone")
                    if best_zone:
                        # text_box is box space; best_zone is a config
                        # zone verbatim (verification.py stores the rule's
                        # own entry), so the union is taken in zone space
                        # and best_zone is NOT converted.
                        tx, ty, tw, th = text_box
                        zy1, zy2, zx1, zx2 = best_zone

                        union_zone = [
                            int(min(ty, zy1)),
                            int(max(ty + th, zy2)),
                            int(min(tx, zx1)),
                            int(max(tx + tw, zx2)),
                        ]

                        suggestions.append({
                            "serial": serial,
                            "action": "EXPAND_ZONE",
                            "original_zone": best_zone,
                            "new_zone": union_zone,
                            "reason": f"Partial leak detected ({f.value}). Expanded to cover."
                        })

                elif l_type == "NEW_LEAK":
                    # Suggest adding the text box as a new zone.
                    # Convert (x, y, w, h) -> [y1, y2, x1, x2], as
                    # discovery.py does before a zone reaches config.
                    tx, ty, tw, th = text_box
                    zone = [int(ty), int(ty + th), int(tx), int(tx + tw)]

                    suggestions.append({
                        "serial": serial,
                        "action": "ADD_ZONE",
                        "zone": list(zone),
                        "reason": f"New leak detected ({f.value}). Added new zone."
                    })

        return suggestions

    @staticmethod
    def apply_suggestions(session: 'DicomSession', suggestions: List[Dict[str, Any]]) -> int:
        """
        Applies the suggestions to the session's in-memory configuration.

        Edits `session.configuration.rules` in place, without saving. A
        suggestion whose serial has no rule, an `ADD_ZONE` whose zone the
        rule already holds, and an `EXPAND_ZONE` whose original zone is no
        longer in the rule are skipped.

        Args:
            session (DicomSession): The session whose configuration is
                changed.
            suggestions (List[Dict[str, Any]]): As returned by
                `suggest_config_updates`.

        Returns:
            int: Number of changes applied.
        """
        count = 0
        rules = session.configuration.rules

        for sug in suggestions:
            serial = sug["serial"]
            action = sug["action"]

            # Find the rule object
            target_rule = None
            for r in rules:
                if r.get("serial_number") == serial:
                    target_rule = r
                    break

            if not target_rule:
                continue

            if action == "ADD_ZONE":
                zone = sug["zone"]
                if zone not in target_rule["redaction_zones"]:
                    target_rule["redaction_zones"].append(zone)
                    count += 1

            elif action == "EXPAND_ZONE":
                old_zone = sug["original_zone"]
                new_zone = sug["new_zone"]

                # Find index of old_zone
                zones = target_rule["redaction_zones"]
                try:
                    # Compared as lists: a zone may be a tuple.
                    idx = -1
                    for i, z in enumerate(zones):
                        if list(z) == list(old_zone):
                            idx = i
                            break

                    if idx >= 0:
                        zones[idx] = new_zone
                        count += 1
                except ValueError:
                    pass

        return count

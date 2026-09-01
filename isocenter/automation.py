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
        """
        Generates a list of suggested configuration changes.

        Returns:
            List[Dict]: A list of 'suggestion' objects:
            {
                "serial": str,
                "action": "ADD_ZONE" | "EXPAND_ZONE",
                "zone": [y1, y2, x1, x2],
                "reason": str
            }

        Zones are emitted in config space -- (y1, y2, x1, x2), the order
        every consumer of ``redaction_zones`` reads. OCR metadata arrives
        in box space -- (x, y, w, h) -- and the conversion happens here,
        mirroring ``discovery.py``: automation used to append raw boxes,
        so an applied suggestion redacted rows x:y and columns w:h -- the
        wrong region, or nothing at all -- while the leak it was created
        for stayed in the image (#258).
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
                # Todo: Handle findings with no matching rule (Unknown Serial or No config entry)
                pass

        for serial, findings in findings_by_serial.items():
            # In a real system we might merge zones here.
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
                        # and best_zone is NOT converted (#258).
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
        Returns: Number of changes applied.
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
                # Check duplicates?
                if zone not in target_rule["redaction_zones"]:
                    target_rule["redaction_zones"].append(zone)
                    count += 1

            elif action == "EXPAND_ZONE":
                old_zone = sug["original_zone"]
                new_zone = sug["new_zone"]

                # Find index of old_zone
                zones = target_rule["redaction_zones"]
                try:
                    # Convert to list for comparison just in case
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

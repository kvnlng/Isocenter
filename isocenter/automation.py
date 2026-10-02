"""
Module for analyzing OCR findings and suggesting configuration updates.
"""
from typing import List, Dict, Any
from collections import defaultdict
from isocenter.privacy import PhiReport
from isocenter.services import zone_rois

def _rule_for_machine(finding, meta, zone):
    """An `ADD_RULE` suggestion for the finding's machine, or None.

    For a leak whose zone would otherwise go to, or grow on, the `"*"`
    rule, which every machine reads (owner rulings Q2-B and on #899).

    Args:
        finding (PhiFinding): The leak, for its text.
        meta (dict): Its metadata, carrying `machine_serial`,
            `manufacturer` and `model_name`.
        zone (list): `[y1, y2, x1, x2]` for the new rule.

    Returns:
        Optional[dict]: The suggestion; None with no machine serial.
    """
    machine = meta.get("machine_serial")
    if not machine:
        return None
    return {
        "serial": machine,
        "action": "ADD_RULE",
        "zone": list(zone),
        "manufacturer": meta.get("manufacturer") or "",
        "model_name": meta.get("model_name") or "",
        # True whether or not the machine already has a rule of its own:
        # `apply_suggestions` grows that rule when it exists.
        "reason": (f"{meta.get('leak_type')} detected ({finding.value}); a "
                   f"zone on the '*' rule would apply to every machine, so "
                   f"the zone goes to a rule for serial {machine}."),
    }


class ConfigAutomator:
    """
    Analyzes OCR findings and generates suggestions to update the redaction configuration.
    """

    @staticmethod
    def suggest_config_updates(report: PhiReport) -> List[Dict[str, Any]]:
        """Generates a list of suggested configuration changes.

        A `NEW_LEAK` finding suggests its text box as a new zone on the
        machine's exact rule; when only a `"*"` rule covers the machine
        (`rule_serial` is `"*"`), it suggests a new exact rule for the
        machine's serial instead (`ADD_RULE`), never a zone on `"*"`,
        which every machine reads. A `PARTIAL_LEAK` finding suggests
        growing its best-matching zone to cover the text, on the rule
        that holds it; when that rule is `"*"`, it suggests a new exact
        rule carrying the grown zone instead (`ADD_RULE`), and `"*"` is
        never widened. A finding with no `rule_serial` in its metadata
        gets no suggestion. Zones are in config space, (y1, y2, x1, x2),
        the order every consumer of ``redaction_zones`` reads; OCR boxes
        arrive as (x, y, w, h) and are converted.

        Args:
            report (PhiReport): Findings from a pixel scan, each carrying
                `leak_type`, `text_box`, `best_zone` and `rule_serial` in
                its metadata, and `machine_serial`, `manufacturer` and
                `model_name` for an `ADD_RULE`.

        Returns:
            List[Dict[str, Any]]: One dict per suggestion, with `serial`,
                `action` and `reason`; an `ADD_ZONE` suggestion carries
                `zone`, an `EXPAND_ZONE` one `original_zone` and
                `new_zone`, each `[y1, y2, x1, x2]`. An `ADD_RULE`
                suggestion carries `zone`, `manufacturer` and
                `model_name`, and its `serial` is the machine's.
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
                        # text_box is box space; best_zone is the zone's
                        # ROI in zone space (verification.py stores the
                        # `[y1, y2, x1, x2]` list `zone_rois` read, never
                        # a dict entry, whose unpacking below would read
                        # its keys), so the union is taken in zone space
                        # and best_zone is NOT converted.
                        tx, ty, tw, th = text_box
                        zy1, zy2, zx1, zx2 = best_zone

                        union_zone = [
                            int(min(ty, zy1)),
                            int(max(ty + th, zy2)),
                            int(min(tx, zx1)),
                            int(max(tx + tw, zx2)),
                        ]

                        if serial == "*":
                            # The zone is the wildcard's: growing it would
                            # redact the grown region on every machine's
                            # images for a leak seen on one. A rule for
                            # this machine carries the grown zone instead,
                            # as for a NEW_LEAK (owner ruling on #899).
                            rule = _rule_for_machine(f, meta, union_zone)
                            if rule is not None:
                                suggestions.append(rule)
                            continue

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

                    if serial == "*":
                        # Only the wildcard covers this machine: a zone on
                        # "*" would redact that region on every machine's
                        # images, so a rule for this one is suggested
                        # instead (owner ruling Q2-B, #808).
                        rule = _rule_for_machine(f, meta, zone)
                        if rule is not None:
                            suggestions.append(rule)
                        continue

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

        Edits `session.configuration.rules` in place, without saving.
        Zones are compared by their ROI (`services.zone_rois`), so
        `[y1, y2, x1, x2]` and `{"roi": [y1, y2, x1, x2]}` are one zone.
        An `EXPAND_ZONE` replaces a list zone with the new list, and a
        dict zone's `roi` in place, keeping its other keys (`note`). An
        `ADD_RULE` appends a rule for the serial (`serial_number`,
        `manufacturer`, `model_name`, `redaction_zones`), or adds its zone
        to the exact rule already there, one an earlier suggestion created
        included. A suggestion whose serial has no rule (other than
        `ADD_RULE`), an `ADD_ZONE` whose zone the rule already holds, and
        an `EXPAND_ZONE` whose original zone is in no rule for its serial
        are skipped.

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

            # Exact on `serial_number`: "*" is a literal value here, the
            # serial of the wildcard rule.
            same_serial = [r for r in rules if r.get("serial_number") == serial]

            if action == "ADD_RULE" and not same_serial:
                # `create_config()`'s shape for a machine; every key is in
                # `config_manager._RULE_KEYS`, so `save()` then
                # `load_config()` reads it back.
                rules.append({
                    "serial_number": serial,
                    "manufacturer": sug.get("manufacturer", ""),
                    "model_name": sug.get("model_name", ""),
                    "redaction_zones": [list(sug["zone"])],
                })
                count += 1
                continue

            if not same_serial:
                continue
            target_rule = same_serial[0]

            if action in ("ADD_ZONE", "ADD_RULE"):
                zone = sug["zone"]
                zones = target_rule.setdefault("redaction_zones", [])
                # By ROI, so a list suggestion beside the dict that already
                # holds it is a duplicate (#814).
                if tuple(zone) not in zone_rois(zones):
                    zones.append(zone)
                    count += 1

            elif action == "EXPAND_ZONE":
                old_roi = tuple(sug["original_zone"])
                new_zone = sug["new_zone"]
                # Every rule on the serial, since two rules may share one
                # and `best_zone` may be in the second; the first zone whose
                # ROI is the original is grown.
                for rule in same_serial:
                    zones = rule.get("redaction_zones") or []
                    idx = next((i for i, z in enumerate(zones)
                                if zone_rois([z]) == [old_roi]), -1)
                    if idx < 0:
                        continue
                    if isinstance(zones[idx], dict):
                        zones[idx]["roi"] = list(new_zone)
                    else:
                        zones[idx] = list(new_zone)
                    count += 1
                    break

        return count

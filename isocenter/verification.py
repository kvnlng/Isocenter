"""Classify burned-in text found by OCR against the configured redaction zones."""
from typing import List, Dict, Any, Tuple
from isocenter.entities import Instance
from isocenter.privacy import PhiFinding
from isocenter.pixel_analysis import analyze_pixels
from isocenter.services import rule_covers, rules_matching, zone_rois

def _serial_of(equipment) -> str:
    """The serial the rules read for `equipment`: `""` for none (#1108)."""
    return (equipment.device_serial_number or "") if equipment else ""


def _rule_names(rule):
    """`(rule_serial, rule_match)` for a finding's metadata: a serial
    rule's `serial_number` and None, or None and a `match:` rule's match
    (#1108)."""
    if "match" in rule:
        return None, dict(rule.get("match") or {})
    return rule.get("serial_number"), None


def _rule_a_new_zone_goes_to(covering, serial):
    """The rule a NEW_LEAK's zone is suggested for, as `_rule_names` names
    it: narrowest first (owner ruling on #1108). An exact serial rule;
    else the first `match:` rule; else, for a series with no serial, a
    `""` rule; else `"*"`, which `ConfigAutomator` turns into a rule for
    the machine's serial (`""` for one with none) rather than a zone on
    `"*"`, which every machine reads (owner ruling Q2-B). `(None, None)`
    when no rule covers it (a direct `verify_instance` call), as before.

    Args:
        covering (list): The rules covering the series, in rule order.
        serial (str): The series' serial, `""` for none.

    Returns:
        tuple: `(rule_serial, rule_match)`.
    """
    if serial:
        if any(r.get("serial_number") == serial and "match" not in r
               for r in covering):
            return serial, None
    for rule in covering:
        if "match" in rule:
            return _rule_names(rule)
    if not serial and any(r.get("serial_number") == "" and "match" not in r
                          for r in covering):
        return "", None
    return ("*", None) if covering else (None, None)


class RedactionVerifier:
    """
    Verifies pixel redaction strategies by comparing OCR results
    against configured redaction zones.
    """

    def __init__(self, rules: List[Dict[str, Any]] = None):
        """
        Args:
            rules (List[Dict]): A list of redaction rules (config['machines']).
        """
        self.rules = rules or []

    def get_matching_rule(self, equipment: Any) -> Dict[str, Any]:
        """
        Finds the first redaction rule that applies to this equipment.

        A rule applies when its `serial_number` is the equipment's Device
        Serial Number, `""` for equipment with none (no equipment
        included), or `"*"`, or when its `match:` names the equipment's
        manufacturer and/or model, as `redact()` reads it
        (`services.rule_covers`, #1108); the first in rule order wins,
        so a `"*"` rule listed before an exact one is returned. Until #808
        only an exact serial matched. The scan itself reads every applying
        rule's zones, not this one rule's.

        Args:
            equipment (Equipment): The instance's equipment, or None.

        Returns:
            Dict[str, Any]: The rule dictionary, or None when no rule
            covers it.
        """
        for rule in self.rules:
            if rule_covers(rule, equipment):
                return rule
        return None

    def _covering_zones(self, equipment: Any) -> List[Tuple[dict, list]]:
        """Every zone that applies to this equipment, with the rule it is in.

        The zones of *every* rule that covers the serial (`rules_matching`),
        in rule order, each read by `zone_rois`, the reader `redact()` and
        the export use: `[y1, y2, x1, x2]` and `{"roi": [...]}` are both
        zones, and an invalid zone is dropped silently (`redact()` warns
        about it once per pass).

        Args:
            equipment (Equipment): The instance's equipment, or None.

        Returns:
            List[Tuple[dict, list]]: `(rule, [y1, y2, x1, x2])` per zone.
        """
        return [(rule, list(roi))
                for rule in rules_matching(self.rules, equipment)
                for roi in zone_rois(rule.get("redaction_zones"))]

    def _coverage(self, text_box: Tuple[int, int, int, int], zone_box: Tuple[int, int, int, int]) -> float:
        """
        Fraction of the text_box's area that zone_box covers (0.0 - 1.0).

        Args:
            text_box (Tuple[int, int, int, int]): OCR box space (x, y, w, h).
            zone_box (Tuple[int, int, int, int]): A config `redaction_zones`
                entry in zone space (y1, y2, x1, x2).

        Returns:
            float: The covered fraction; 0.0 for no overlap or an empty box.
        """
        # The two boxes deliberately speak different conventions, and the
        # conversion happens here and nowhere else on the read side. Zone
        # space is the order every consumer that touches pixels reads
        # (`apply_redaction_to_array`, both redact paths, the export
        # worker); reading the zone as (x, y, w, h) here would make the
        # classifier disagree with redaction about what every zone covers.
        tx, ty, tw, th = text_box
        zy1, zy2, zx1, zx2 = zone_box

        # Calculate Intersection
        x_left = max(tx, zx1)
        y_top = max(ty, zy1)
        x_right = min(tx + tw, zx2)
        y_bottom = min(ty + th, zy2)

        if x_right <= x_left or y_bottom <= y_top:
            return 0.0

        text_area = tw * th
        if text_area <= 0:
            return 0.0

        intersection_area = (x_right - x_left) * (y_bottom - y_top)
        return intersection_area / text_area

    def is_covered(self, text_box: Tuple[int, int, int, int], zone_box: Tuple[int, int, int, int], threshold=0.50) -> bool:
        """Checks if the text_box is significantly covered by the zone_box.

        Args:
            text_box: OCR box space (x, y, w, h).
            zone_box: zone space (y1, y2, x1, x2), as `redaction_zones`
                entries are stored and as redaction applies them.
            threshold (float): Fraction of text area that must be covered
                (0.0 - 1.0).

        Returns:
            bool: True if covered.
        """
        return self._coverage(text_box, zone_box) >= threshold

    def verify_instance(self, instance: Instance, equipment: Any = None) -> List[PhiFinding]:
        """Runs OCR on the instance and classifies each text region.

        - If text is fully matched (>= 80% coverage): considered Safe (Ignored).
        - If text is partially matched (> 0% but < 80%): Reported as PARTIAL_LEAK.
        - If text is not matched (0%): Reported as NEW_LEAK.

        Text of two characters or fewer is skipped as noise.

        Args:
            instance (Instance): The instance to read.
            equipment (Equipment, optional): Selects the redaction rule by
                serial number; with none, every region is a NEW_LEAK.

        Returns:
            List[PhiFinding]: One finding per leaked region. Also `[]` when
            OCR is unavailable, which is not "nothing leaks":
            `Session.scan_pixel_content()` checks first and refuses
            instead.
        """
        return self._findings_for(instance, analyze_pixels(instance), equipment)

    def _findings_for(self, instance: Instance, text_regions: List[Any],
                      equipment: Any = None) -> List[PhiFinding]:
        """Classify already-read OCR regions against every covering rule's zones.

        Text of two characters or fewer is skipped as noise.

        Args:
            instance (Instance): The instance the regions were read from.
            text_regions (List[Any]): `TextRegion`s from OCR.
            equipment (Equipment, optional): Selects the redaction rules
                (`services.rules_matching`).

        Returns:
            List[PhiFinding]: One finding per region not covered by at least
            80% by a zone.
        """
        # Split out of `verify_instance` so the Session worker can read the
        # instance through `pixel_analysis._ocr_instance`, which reports a
        # failed load or frame, and still classify here: `verify_instance`
        # reads through `analyze_pixels`, which can only log one.
        if not text_regions:
            return []

        # Every zone of every rule that covers this machine, as `redact()`
        # applies them (#808, #814). `best_zone` is the ROI as a 4-list,
        # never the rule's raw entry, which may be a dict.
        zones = self._covering_zones(equipment)
        serial = _serial_of(equipment)
        new_leak_rule = _rule_a_new_zone_goes_to(
            rules_matching(self.rules, equipment), serial)

        findings = []

        for region in text_regions:
            best_coverage = 0.0
            best_zone = None
            best_rule = None

            # Check against all zones to find BEST coverage. The zone
            # convention ((y1, y2, x1, x2), unlike region.box's
            # (x, y, w, h)) lives in _coverage; do not inline the math
            # here with an unpacking of its own.
            for rule, roi in zones:
                cov = self._coverage(region.box, tuple(roi))
                if cov > best_coverage:
                    best_coverage = cov
                    best_zone = roi
                    best_rule = rule

            threshold_safe = 0.80

            clean_text = region.text.replace('\n', ' ').strip()
            if len(clean_text) <= 2:
                continue # Skip noise

            if best_coverage >= threshold_safe:
                # Safe, ignore
                continue

            # It's a finding. `rule_serial` names the rule a suggestion
            # edits: the one whose zone is grown for a partial leak, the
            # exact rule that takes a new zone for a new one.
            # A `match:` rule is named by `rule_match`, with `rule_serial`
            # None (#1108).
            if best_coverage > 0.0:
                reason = "Partial Leak"
                f_type = "PARTIAL_LEAK"
                rule_serial, rule_match = _rule_names(best_rule)
            else:
                reason = "New Leak (Uncovered)"
                f_type = "NEW_LEAK"
                rule_serial, rule_match = new_leak_rule

            f = PhiFinding(
                entity_uid=instance.sop_instance_uid,
                entity_type="Instance",
                field_name=f"PixelData[Frame={region.frame_index}]",
                value=clean_text,
                reason=f"{reason} (Cov: {best_coverage:.2f})",
                entity=instance,
                metadata={
                    "leak_type": f_type,
                    "coverage_score": best_coverage,
                    "text_box": region.box,  # (x, y, w, h)
                    "best_zone": best_zone,
                    "rule_serial": rule_serial,
                    "rule_match": rule_match,
                    # Plain strings, so they cross a process boundary;
                    # what a rule created for this machine is written
                    # with (`create_config()`'s shape).
                    "machine_serial": serial,
                    "manufacturer": getattr(equipment, "manufacturer", None),
                    "model_name": getattr(equipment, "model_name", None),
                }
            )
            findings.append(f)

        return findings

from typing import List, Dict, Any, Tuple
from isocenter.entities import Instance
from isocenter.privacy import PhiFinding
from isocenter.pixel_analysis import analyze_pixels

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
        Finds the redaction rule that applies to this equipment.
        Uses exact Serial Number match first, then Model/Manufacturer logic.
        """
        if not equipment:
            return None

        target_serial = equipment.device_serial_number
        if not target_serial:
            return None

        # 1. Exact Serial Match
        for rule in self.rules:
            if rule.get("serial_number") == target_serial:
                return rule

        # 2. Check Model/Manufacturer (if serial not found or not required by rule?)
        # For verification, we stick to strict serial matching as per current architecture
        # unless there's a fallback mechanism.
        # For now, strict match.
        return None

    def is_covered(self, text_box: Tuple[int, int, int, int], zone_box: Tuple[int, int, int, int], threshold=0.50) -> bool:
        """
        Checks if the text_box is significantly covered by the zone_box.

        Args:
            text_box: (x, y, w, h)
            zone_box: (x, y, w, h)
            threshold: Fraction of text area that must be covered (0.0 - 1.0).

        Returns:
            bool: True if covered.
        """
        tx, ty, tw, th = text_box
        zx, zy, zw, zh = zone_box

        # Calculate Intersection
        x_left = max(tx, zx)
        y_top = max(ty, zy)
        x_right = min(tx + tw, zx + zw)
        y_bottom = min(ty + th, zy + zh)

        if x_right < x_left or y_bottom < y_top:
            return False

        intersection_area = (x_right - x_left) * (y_bottom - y_top)
        text_area = tw * th

        if text_area == 0:
            return False

        coverage = intersection_area / text_area
        return coverage >= threshold

    def verify_instance(self, instance: Instance, equipment: Any = None) -> List[PhiFinding]:
        """
        Runs OCR on the instance.
        - If text is fully matched (>= 80% coverage): considered Safe (Ignored).
        - If text is partially matched (> 0% but < 80%): Reported as PARTIAL_LEAK.
        - If text is not matched (0%): Reported as NEW_LEAK.
        """
        text_regions = analyze_pixels(instance)

        if not text_regions:
            return []

        rule = self.get_matching_rule(equipment)
        zones = []
        if rule:
            raw_zones = rule.get("redaction_zones", [])
            zones = raw_zones

        findings = []

        for region in text_regions:
            best_coverage = 0.0
            best_zone = None

            # Check against all zones to find BEST coverage
            for zone in zones:
                if len(zone) >= 4:
                    z_box = (zone[0], zone[1], zone[2], zone[3])

                    # Calculate logic manually here or reuse is_covered logic but return float?
                    # Let's inline the area math or split helper.

                    tx, ty, tw, th = region.box
                    zx, zy, zw, zh = z_box

                    x_left = max(tx, zx)
                    y_top = max(ty, zy)
                    x_right = min(tx + tw, zx + zw)
                    y_bottom = min(ty + th, zy + zh)

                    if x_right > x_left and y_bottom > y_top:
                        intersection_area = (x_right - x_left) * (y_bottom - y_top)
                        text_area = tw * th
                        if text_area > 0:
                            cov = intersection_area / text_area
                            if cov > best_coverage:
                                best_coverage = cov
                                best_zone = zone

            # Decision Logic
            threshold_safe = 0.80  # Configurable?

            clean_text = region.text.replace('\n', ' ').strip()
            if len(clean_text) <= 2:
                continue # Skip noise

            if best_coverage >= threshold_safe:
                # Safe, ignore
                continue

            # It's a finding
            if best_coverage > 0.0:
                reason = "Partial Leak"
                f_type = "PARTIAL_LEAK"
            else:
                reason = "New Leak (Uncovered)"
                f_type = "NEW_LEAK"

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
                    "rule_serial": rule.get("serial_number") if rule else None
                }
            )
            findings.append(f)

        return findings

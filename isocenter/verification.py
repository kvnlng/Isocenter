"""Classify burned-in text found by OCR against the configured redaction zones."""
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

        Exact Serial Number match only; the first matching rule wins.

        Args:
            equipment (Equipment): The instance's equipment, or None.

        Returns:
            Dict[str, Any]: The rule dictionary, or None when there is no
                equipment, no serial, or no rule for it.
        """
        if not equipment:
            return None

        target_serial = equipment.device_serial_number
        if not target_serial:
            return None

        for rule in self.rules:
            if rule.get("serial_number") == target_serial:
                return rule

        # Strict serial matching only: no model/manufacturer fallback.
        return None

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
        """Classify already-read OCR regions against the matching rule.

        Text of two characters or fewer is skipped as noise.

        Args:
            instance (Instance): The instance the regions were read from.
            text_regions (List[Any]): `TextRegion`s from OCR.
            equipment (Equipment, optional): Selects the redaction rule.

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

        rule = self.get_matching_rule(equipment)
        zones = []
        if rule:
            raw_zones = rule.get("redaction_zones", [])
            zones = raw_zones

        findings = []

        for region in text_regions:
            best_coverage = 0.0
            best_zone = None

            # Check against all zones to find BEST coverage. The zone
            # convention ((y1, y2, x1, x2), unlike region.box's
            # (x, y, w, h)) lives in _coverage; do not inline the math
            # here with an unpacking of its own.
            for zone in zones:
                if len(zone) >= 4:
                    cov = self._coverage(region.box, tuple(zone[:4]))
                    if cov > best_coverage:
                        best_coverage = cov
                        best_zone = zone

            threshold_safe = 0.80

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

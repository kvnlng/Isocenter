"""Convert a CTP DicomPixelAnonymizer.script into Isocenter redaction rules."""
import os
import re
import sys
import yaml


class CTPParser:
    """
    Parses CTP DicomPixelAnonymizer.script files into Isocenter-compatible rules.
    """

    @staticmethod
    def parse_script(content: str):
        """Parse a script's text into machine redaction rules.

        Each `{ condition }` block followed by `(x,y,w,h)` coordinates gives
        one rule when its condition names a Manufacturer or a
        ManufacturerModelName (`containsIgnoreCase`); other blocks are
        skipped. Coordinates are converted to zone space `[y, y+h, x, x+w]`.

        Args:
            content (str): The script's text.

        Returns:
            list: Rule dicts with `manufacturer`, `model_name`, `comment`
                and `redaction_zones` keys, in script order.
        """
        rules = []

        # The format is roughly:
        #   Title/Comment (Lines)
        #   { condition }
        #   (x,y,w,h) ...
        # A condition block and its coordinates may each span several lines.

        content = content.replace('\r\n', '\n')

        pattern = re.compile(r'\{\s*(.*?)\s*\}\s*([\(\)\d\s,]+)', re.DOTALL)

        matches = pattern.findall(content)

        for condition_str, coords_str in matches:
            rule = CTPParser._parse_block(condition_str, coords_str)
            if rule:
                rules.append(rule)

        return rules

    @staticmethod
    def _parse_block(condition_str, coords_str):
        # 1. Parse Condition to extract Match Criteria
        criteria = {}

        # Examples:
        # Manufacturer.containsIgnoreCase("GE MEDICAL")
        # ManufacturerModelName.containsIgnoreCase("Aquilion ONE")
        # Rows.equals("512")

        # Extract Manufacturer
        m_man = re.search(r'Manufacturer\.containsIgnoreCase\("([^"]+)"\)', condition_str)
        if m_man:
            criteria['manufacturer'] = m_man.group(1)

        # Extract Model
        # ManufacturerModelName can be mapped to model_name
        m_mod = re.search(r'ManufacturerModelName\.containsIgnoreCase\("([^"]+)"\)', condition_str)
        if m_mod:
            criteria['model_name'] = m_mod.group(1)

        # No serial number is read: CTP scripts target Modality/Model.

        if not criteria:
            pass

        # 2. Parse Coordinates
        # (x,y,w,h)
        # Isocenter expects: [r1, r2, c1, c2] = [y, y+h, x, x+w]

        isocenter_zones = []

        # Find all (x,y,w,h) tuples
        coord_matches = re.findall(
            r'\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)', coords_str)

        for (x, y, w, h) in coord_matches:
            x, y, w, h = int(x), int(y), int(w), int(h)
            isocenter_zone = [y, y + h, x, x + w]
            isocenter_zones.append(isocenter_zone)

        if not isocenter_zones:
            return None

        # A block with neither a manufacturer nor a model gives no rule.

        if 'manufacturer' in criteria or 'model_name' in criteria:
            return {
                "manufacturer": criteria.get("manufacturer", "Unknown"),
                "model_name": criteria.get("model_name", "Unknown"),
                "comment": f"Imported from CTP. Condition: {condition_str.strip()}",
                "redaction_zones": isocenter_zones
            }

        return None


if __name__ == "__main__":

    if len(sys.argv) < 3:
        print("Usage: python -m isocenter.utils.ctp_parser <input_script_path> <output_yaml_path>")
        sys.exit(1)

    input_path = sys.argv[1]
    output_path = sys.argv[2]

    if not os.path.exists(input_path):
        print(f"Error: Input file {input_path} not found.")
        sys.exit(1)

    try:
        with open(input_path, 'r') as f:
            content = f.read()

        rules = CTPParser.parse_script(content)

        output_data = {"rules": rules}

        with open(output_path, 'w') as f:
            yaml.dump(output_data, f, sort_keys=False, default_flow_style=False)

        print(f"Successfully converted {len(rules)} rules to {output_path}")

    except Exception as e:
        print(f"Error parsing script: {e}")
        sys.exit(1)

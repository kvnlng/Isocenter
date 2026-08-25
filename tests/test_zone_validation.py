
import pytest
from isocenter.config_manager import ConfigLoader

def test_zone_validation_flexible_format(tmp_path):
    """
    Regression Test: Ensures that redaction zones can be specified EITHER as
    a list of integers [y1, y2, x1, x2] OR as a dictionary {"roi": ...}.
    This supports both manual configuration and automated CTP imports.
    """

    # 1. Test List Format (e.g. from CTP)
    list_zone_rule = {
        "serial_number": "TEST_LIST_ZONE",
        "redaction_zones": [
            [10, 60, 10, 60]
        ]
    }
    # Should not crash
    ConfigLoader._validate_rule(list_zone_rule, 0)

    # 2. Test Dict Format (Standard Isocenter)
    dict_zone_rule = {
        "serial_number": "TEST_DICT_ZONE",
        "redaction_zones": [
            {"roi": [10, 60, 10, 60], "label": "Test Zone"}
        ]
    }
    # Should not crash
    ConfigLoader._validate_rule(dict_zone_rule, 1)

    # 3. Test Invalid Format
    invalid_rule = {
        "serial_number": "TEST_BAD",
        "redaction_zones": ["bad_string"]
    }
    with pytest.raises(ValueError, match="Invalid zone format"):
        ConfigLoader._validate_rule(invalid_rule, 2)

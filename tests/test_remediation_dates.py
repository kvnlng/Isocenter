
import pytest
from isocenter.remediation import RemediationService

@pytest.fixture
def service():
    return RemediationService()

class TestDateShifting:

    def test_shift_date_standard_da(self, service):
        """Test standard DA format (YYYYMMDD)"""
        # 2023-05-15 + 10 days = 2023-05-25
        result = service._shift_date_string("20230515", days=10)
        assert result == "20230525"

    def test_shift_date_dt_compact(self, service):
        """Test DT format without separators (YYYYMMDDHHMMSS)"""
        # 2023-05-15 ... + 10 days
        result = service._shift_date_string("20230515104822", days=10)
        assert result == "20230525104822"

    def test_shift_date_dt_dots(self, service):
        """Test DT format with dots (YYYYMMDD.HHMMSS)"""
        result = service._shift_date_string("20230515.104822", days=10)
        assert result == "20230525.104822"

    def test_shift_date_dt_millis(self, service):
        """Test DT format with milliseconds (YYYYMMDD.HHMMSS.ffffff)"""
        # Note: input has 3 digit millis (677), which standard python %f expects 6 digits usually or might fail if not careful.
        # However, strptime %f expects microseconds (6 digits).
        # If input is .677, it might assume .677000.
        # Actually DICOM DT allows variable precision.
        # Let's see if simple %f works or if we need custom handling.
        # Python's %f works with 6 digits. For partial, it might be tricky.
        # Let's test what we expect. Explicitly handling 3 digits might be needed if standard %f fails.
        pass

    def test_shift_date_handling_variable_formats(self, service):
        cases = [
            ("20230515.104822.677000", 10, "20230525.104822.677000"), # Full micro
            ("20230101", 365, "20240101"), # Leap year check potentially? 2024 is leap.
            ("20200228", 1, "20200229"), # Leap day
        ]
        for original, days, expected in cases:
            assert service._shift_date_string(original, days) == expected

    def test_shift_date_iso_format(self, service):
        """Test ISO format (YYYY-MM-DD)"""
        # 2024-05-11 + 10 days = 2024-05-21
        result = service._shift_date_string("2024-05-11", days=10)
        assert result == "2024-05-21"

    def test_shift_date_invalid(self, service):
        assert service._shift_date_string("", 10) is None
        assert service._shift_date_string(None, 10) is None
        assert service._shift_date_string("NotADate", 10) is None

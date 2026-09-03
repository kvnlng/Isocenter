
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

    def test_a_fractional_second_the_format_list_cannot_parse_still_shifts_its_date(
            self, service):
        """A DT whose fraction `%f` rejects must still be jittered.

        This replaces a stub that asserted nothing (its body was
        comments ending in `pass`) and is the question that stub was
        reaching for. DICOM DT allows a fraction of any precision;
        `strptime`'s `%f` accepts one to six digits and nothing more, so
        a seven-digit fraction misses every entry in the `formats` loop
        and reaches the dotted-DT fallback below it, which shifts the
        first eight characters and re-attaches the remainder verbatim.

        Without the `len(parts) >= 3` guard holding, that input returns
        None; `_apply_single_remediation` then takes its `else` arm,
        logs "Invalid date format", and returns having shifted nothing
        -- so the real date survives in the graph, is exported, and no
        exception and no audit row says it happened.

        The PAIR is the point, and specifically the DIFFERING fractions.
        The first input IS parsed by the `formats` loop, which normalises
        its fraction to six digits; the second comes back with all seven
        preserved, which is only possible via the fallback. That is what
        proves the second input genuinely reached the code under test
        rather than passing vacuously -- and it means a future addition
        to `formats` that swallowed the second input would flip its
        expected value and fail loudly instead of going quiet.
        """
        # Parsed by the formats loop: fraction normalised to six digits.
        assert service._shift_date_string(
            "20230515.104822.677", 10) == "20230525.104822.677000"

        # Seven digits: rejected by %f, so this one reaches the fallback
        # and keeps its fraction exactly as written.
        assert service._shift_date_string(
            "20230515.104822.1234567", 10) == "20230525.104822.1234567"

    def test_a_malformed_date_part_is_declined_rather_than_shifted_into_a_fabricated_one(
            self, service):
        """The dotted-DT fallback's length check is not redundant.

        `strptime` with `%Y%m%d` is NOT length-strict -- `"2023051"`
        parses as 2023-05-01 and `"230515"` as 2305-01-05, raising
        nothing. So `parts[0].isdigit()` alone lets a wrong-length date
        part through to `strptime`, and the branch then shifts a date
        nobody wrote and re-attaches `date_str[8:]`, which is misaligned
        for any length but eight.

        That makes `and -> or` at the guard a DISTINGUISHABLE mutant,
        and the first pass of #132 got this wrong: it was classified as
        equivalent on the reasoning that any bad `parts[0]` would raise
        ValueError and fall through to the same `return None`. Measured
        with `or` substituted, it does not:

            "2023051.104822.1234567" -> "20230511104822.1234567"
            "230515.104822.1234567"  -> "2305011504822.1234567"

        Both are fabricated values that still look like a DT, produced
        from input the real code declines. The failure is a shape worse
        than the one at line 392: not a real date left unshifted, but a
        plausible-looking date invented and written into the graph as
        though the shift had succeeded.
        """
        assert service._shift_date_string("2023051.104822.1234567", 10) is None
        assert service._shift_date_string("230515.104822.1234567", 10) is None

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

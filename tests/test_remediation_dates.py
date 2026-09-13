
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
        """A dotted DT's fraction is re-attached as written, whatever its
        length.

        This replaced a stub that asserted nothing, and was a PAIR: a
        three-digit fraction the old strptime loop parsed (and re-rendered
        as `.677000`) beside a seven-digit one `%f` rejected, which only
        the old dotted fallback kept verbatim. Since #559 there is no
        loop and no fallback -- the date moves and the rest of the string
        is re-attached -- so both keep their fraction exactly, and the
        seven-digit one proves the dotted shape still accepts a fraction
        longer than DT's six (it did before, and the accept set only
        narrows by the fabricating shapes).

        Declining it would leave the real date in the graph with only a
        decline row to say so.
        """
        assert service._shift_date_string(
            "20230515.104822.677", 10) == "20230525.104822.677"
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


#: (input, days, expected) for the length-strict parser (#559). Measured
#: on ac33641 with days=-10 unless the row says otherwise; the comment on
#: each changed row gives what the old strptime loop returned.
SHIFT_TABLE = [
    # DA: unchanged.
    ("20230515", -10, "20230505"),
    ("2023-05-11", -10, "2023-05-01"),
    ("19991231", -10, "19991221"),
    ("99991231", -10, "99991221"),
    ("20200228", 1, "20200229"),
    # ISO with an unpadded month or day parsed before and still does.
    ("2024-5-1", -10, "2024-04-21"),
    # Years below 1000 render with four digits on every platform; strftime
    # does on macOS and not on Linux.
    ("10000101", -10, "09991222"),
    ("09990101", 10, "09990111"),
    # A shift out of year 1 declines.
    ("00010105", -10, None),
    # Declined before and after.
    ("20230515\\20230516", -10, None),
    ("20230515-20230601", -10, None),
    ("202305", -10, None),
    ("00000000", -10, None),
    ("12345678", -10, None),
    ("1CT1", -10, None),
    ("ANONYMIZED", -10, None),
    ("0727", -10, None),
    ("07", -10, None),
    ("072731.1", -10, None),
    ("07:27:31", -10, None),
    ("235959.999999", -10, None),
    ("123000", -10, None),
    ("2023", -10, None),
    ("20230515104822+0100", -10, None),
    ("20230515104822.123456-0500", -10, None),
    ("202305151048+0000", -10, None),
    ("20230515256000", -10, None),
    ("20230515104860", -10, None),
    ("20230515104822.1234567", -10, None),
    # Newly declined: the fabricating shapes.
    ("2023051", -10, None),              # was '20230421'
    ("230515", -10, None),               # was '23041226'
    ("072731", -10, None),               # TM, was '07270219'
    ("072731.123456", -10, None),        # TM, was '07270219.123456'
    ("2023051525", -10, None),           # hour 25
    # DT: the date moves, the time is re-attached as written.
    ("20230515104822", -10, "20230505104822"),
    ("20230515104822.123456", -10, "20230505104822.123456"),
    ("2023051510", -10, "2023050510"),         # was '20230421050100'
    ("202305151048", -10, "202305051048"),     # was '20230505100408'
    ("20230515104822.1", -10, "20230505104822.1"),  # was '.100000'
    ("20230515.104822", -10, "20230505.104822"),
    ("20230515.104822.677", -10, "20230505.104822.677"),  # was '.677000'
    ("20230515.104822.1234567", 10, "20230525.104822.1234567"),
    ("20230515.1048", -10, None),
    ("2023-05-11 10:48:22", -10, "2023-05-01 10:48:22"),
    ("2023-05-11T10:48:22", -10, "2023-05-01T10:48:22"),
    ("2023-05-11T25:48:22", -10, None),
]


@pytest.mark.parametrize("value,days,expected", SHIFT_TABLE,
                         ids=[f"{v}|{d}" for v, d, _ in SHIFT_TABLE])
def test_the_shift_parser_table(service, value, days, expected):
    """#559. `%Y%m%d` is not length-strict and each branch re-rendered
    with `strftime(fmt)`, so a Study Time became a date and an
    hour-precision DateTime came back with its date and time both wrong.
    Kills: `\\d{8}` loosened to `\\d{7,8}`; the DT time re-rendered rather
    than re-attached; the clock-time check deleted; strftime restored
    (the year-0999 rows on Linux); a fraction re-rendered."""
    assert service._shift_date_string(value, days) == expected


def _old_parser(value, days):
    """`_shift_date_string` as it stood on ac33641, the oracle for the
    subset test below: the strptime loop and the dotted fallback."""
    import datetime as _dt
    formats = ["%Y%m%d", "%Y-%m-%d", "%Y%m%d%H%M%S", "%Y%m%d.%H%M%S",
               "%Y%m%d%H%M%S.%f", "%Y%m%d.%H%M%S.%f", "%Y-%m-%d %H:%M:%S",
               "%Y-%m-%dT%H:%M:%S"]
    date_str = str(value).strip()
    for fmt in formats:
        try:
            parsed = _dt.datetime.strptime(date_str, fmt)
            return (parsed + _dt.timedelta(days=days)).strftime(fmt)
        except ValueError:
            continue
    parts = date_str.split(".")
    if len(parts) >= 3 and len(parts[0]) == 8 and parts[0].isdigit():
        try:
            parsed = _dt.datetime.strptime(parts[0], "%Y%m%d")
        except ValueError:
            return None
        return (parsed + _dt.timedelta(days=days)).strftime("%Y%m%d") + date_str[8:]
    return None


def test_accepted_after_is_a_subset_of_accepted_before(service):
    """The accept set only narrows (#559): every value the new parser
    shifts, the old one shifted too -- but for DT at hour or minute
    precision, which it shifted only when it could misread -- and the new
    date part is the value's
    own date moved by the offset. Generated over every prefix of a few
    digit runs (the real DT and the TM shapes that fabricated), each with
    the dotted, fractional and ISO spellings. Kills a widened accept set,
    which the legacy scan branch would read as "already shifted" and skip
    (`_date_shift_declines`)."""
    import datetime as _dt
    import itertools
    seeds = ["20230515104822123456", "07273112345678", "23051510482299",
             "10000101000000", "20231231235959"]
    values = set()
    for seed in seeds:
        for length in range(1, len(seed) + 1):
            values.add(seed[:length])
            values.add(seed[:8] + "." + seed[8:length])
            values.add(seed[:14] + "." + seed[14:length])
            values.add(seed[:8] + "." + seed[8:14] + "." + seed[14:length])
    for y, m, d in itertools.product(("2023", "0999"), ("5", "05", "13"), ("1", "01", "32")):
        values.add(f"{y}-{m}-{d}")
        values.add(f"{y}-{m}-{d} 10:48:22")
        values.add(f"{y}-{m}-{d}T1:2:3")
    for value in sorted(values):
        new = service._shift_date_string(value, -10)
        if new is None:
            continue
        # A DT at hour or minute precision is the one widening: the old
        # loop accepted such a value only when `%H%M%S` could misread its
        # digits (`2023051510` -> `...050100`), and now shifts it right.
        assert (_old_parser(value, -10) is not None
                or (value.isdigit() and len(value) in (10, 12))), value
        if "-" in value[:10]:
            y, m, d = (int(x) for x in value.split(" ")[0].split("T")[0].split("-"))
            assert new[:10] == (_dt.date(y, m, d) - _dt.timedelta(days=10)).isoformat(), value
        else:
            moved = (_dt.datetime.strptime(value[:8], "%Y%m%d")
                     - _dt.timedelta(days=10))
            assert new[:8] == f"{moved.year:04d}{moved.month:02d}{moved.day:02d}", value
            assert new[8:] == value[8:], value


def test_a_jitter_on_a_private_tm_declines_and_is_exported_unchanged(tmp_path, monkeypatch):
    """End to end, the shape #559 found: a JITTER rule on a private
    element that holds a time. On ac33641 the TM `072731` was written back
    as `07261207`, a date-shaped value pydicom warned about in the export
    worker. Now the arm declines it with one row, and the file keeps the
    time. Private, so no load-time VR check stands in front of it (the
    exporter re-VRs private values, and the parser decides by shape).
    Kills the TM shape accepted by the parser."""
    import sqlite3

    import pydicom
    import pydicom.data
    import yaml

    from isocenter.session import DicomSession

    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    source = tmp_path / "in"
    source.mkdir()
    ds = pydicom.dcmread(pydicom.data.get_testdata_file("CT_small.dcm"))
    ds.add_new(0x00290010, "LO", "ACME 1.0")
    ds.add_new(0x00291014, "TM", "072731")
    ds.save_as(str(source / "ct.dcm"))
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(yaml.safe_dump({
        "privacy_profile": "none", "remove_private_tags": False,
        "phi_tags": {"0029,1014": {"action": "JITTER"}}}), encoding="utf-8")

    db = tmp_path / "s.db"
    with DicomSession(str(db)) as session:
        session.ingest(str(source))
        session.load_config(str(cfg))
        session.anonymize(session.audit())
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        assert instance.attributes["0029,1014"] == "072731"
        summary = session.export(str(tmp_path / "out"), use_compression=False)
    assert summary.written == 1, summary.failures
    with sqlite3.connect(str(db)) as conn:
        declines = [row[0] for row in conn.execute(
            "SELECT details FROM audit_log WHERE action_type='REMEDIATION_DECLINED'")]
    assert len(declines) == 1 and "0029,1014" in declines[0], declines
    (written,) = list((tmp_path / "out").rglob("*.dcm"))
    value = pydicom.dcmread(str(written))[0x00291014].value
    # An uncompressed export is Implicit VR, so a private element whose
    # creator pydicom does not know reads back as UN bytes.
    if isinstance(value, bytes):
        value = value.decode("ascii").strip()
    assert value == "072731"

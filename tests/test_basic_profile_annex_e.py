"""`BASIC_PROFILE` is PS3.15 Table E.1-1, row by row (#547).

Until 0.9.8 the basic profile was a hand-picked 35 of the table's 656
rows, commented "Reduced for common usage". Measured on 0.9.7 against
CT_small carrying one identifier of each kind the table names, 25 of 26
reached a bare session's export. A curated subset is how that happened:
nothing held the subset to the table, so nothing said what was left out.

Now the table is a vendored fixture, `tests/support/annex_e.py` says how
each code becomes an action and names every departure with its reason,
and the first test below holds the literal in `isocenter/profiles.py`
equal to that derivation. Editing one entry by hand, dropping one,
adding one the table does not have, or adding a departure without a
reason turns this file red.
"""
import collections
import pathlib
import re

from isocenter.profiles import BASIC_PROFILE, FLOOR_POLICY, RESEARCH_DEFAULTS

from support.annex_e import (ACTION_FOR_CODE, DEVIATIONS, EDITION, NO_ENTRY,
                             NO_ENTRY_CODES, derive, load_table)

REPO = pathlib.Path(__file__).resolve().parent.parent


def test_the_fixture_is_the_named_edition():
    """The row count and the code histogram were read off the 2026c table
    when it was vendored. Kills a truncated fixture, a hand-edited code,
    and a fixture swapped without renaming the edition."""
    table = load_table()
    keys = [row["key"] for row in table["rows"]]

    assert table["edition"] == EDITION == "2026c"
    assert "part15" in table["source"]
    assert len(keys) == 656
    assert len(set(keys)) == 656
    assert collections.Counter(row["basic"] for row in table["rows"]) == {
        "X": 416, "D": 93, "U": 55, "Z": 42, "X/D": 23, "X/Z": 11,
        "X/Z/D": 8, "Z/D": 6, "X/Z/U*": 2}
    assert sum(row["retired"] for row in table["rows"]) == 102
    assert all(key == key.lower() for key in keys)


def test_basic_profile_is_derived_from_annex_e():
    """Kills any single entry deleted, any action flipped, any key the
    table does not name, and a name edited by hand."""
    derived = derive(load_table())

    missing = sorted(set(derived) - set(BASIC_PROFILE))
    extra = sorted(set(BASIC_PROFILE) - set(derived))
    differ = sorted(key for key in set(derived) & set(BASIC_PROFILE)
                    if derived[key] != BASIC_PROFILE[key])
    assert not (missing or extra or differ), (
        f"BASIC_PROFILE has drifted from PS3.15 {EDITION} Table E.1-1: "
        f"missing {missing}, not in the table {extra}, different {differ}. "
        "Change tests/support/annex_e.py and regenerate the literal; do "
        "not edit one entry.")
    assert BASIC_PROFILE == derived

    assert len(BASIC_PROFILE) == 620
    assert collections.Counter(rule["action"] for rule in BASIC_PROFILE.values()) == {
        "REMOVE": 466, "EMPTY": 154}


def test_the_floor_overrides_three_basic_rules_and_adds_none():
    """Patient's Age is a basic rule since 0.9.8, so all three research
    defaults override one and the floor is the profile's size."""
    assert set(RESEARCH_DEFAULTS) <= set(BASIC_PROFILE)
    assert len(FLOOR_POLICY) == len(BASIC_PROFILE) == 620


def test_every_departure_is_a_row_and_is_a_departure():
    """A departure names a row the table has, carries a reason, and
    changes what the mapping alone would give -- a "deviation" equal to
    the mapping is a reason written for nothing, and a no-entry row whose
    code gives no rule anyway hides nothing.

    Kills a stale key left behind by a table refresh, and a blank reason."""
    rows = {row["key"]: row for row in load_table()["rows"]}

    for key, reason in NO_ENTRY.items():
        assert key in rows, f"NO_ENTRY names {key}, which the table does not"
        assert reason.strip(), key
        assert rows[key]["basic"] in ACTION_FOR_CODE, (
            f"{key} is {rows[key]['basic']}, which gives no rule already")
    for code, reason in NO_ENTRY_CODES.items():
        assert code not in ACTION_FOR_CODE and reason.strip()

    for key, deviation in DEVIATIONS.items():
        assert key in rows, f"DEVIATIONS names {key}, which the table does not"
        assert key not in NO_ENTRY
        assert deviation["reason"].strip(), key
        assert deviation["action"] != ACTION_FOR_CODE[rows[key]["basic"]], (
            f"{key}: the deviation gives what the table's code gives")


def test_every_deviation_names_its_reason_and_issue():
    """Each departure is owned: an issue number that its reason repeats, or
    a `path::symbol` naming the code that forces it, which must still be
    there. Kills a silent deviation added to make the derivation test pass,
    and one whose excuse has since been deleted from the code."""
    for key, deviation in DEVIATIONS.items():
        authority = deviation["authority"]
        if re.fullmatch(r"#\d+", authority):
            assert authority in deviation["reason"], (key, authority)
            continue
        path, _, symbol = authority.partition("::")
        source = REPO / path
        assert source.is_file() and symbol, (key, authority)
        assert symbol in source.read_text(encoding="utf-8"), (
            f"{key}'s deviation rests on {symbol} in {path}, which is gone")


def test_the_entity_owned_three_are_the_ones_537_decides():
    """#537 changes these three rules in one place. Kills one dropped."""
    assert {key for key, d in DEVIATIONS.items() if d["authority"] == "#537"} == {
        "0010,0010", "0010,0020", "0008,0020"}


def test_the_configuration_page_names_the_fixture_edition():
    """The docs say which edition the profile is. A refresh that changes
    the fixture and not the page is red here.

    Kills: the page naming 2026b (or nothing) while the fixture says 2026c."""
    text = (REPO / "docs" / "configuration.md").read_text(encoding="utf-8")

    named = set(re.findall(r"\b(20\d\d[a-e])\b", text))
    assert named == {EDITION}, (
        f"docs/configuration.md names edition(s) {sorted(named)}; the "
        f"fixture is {EDITION}")

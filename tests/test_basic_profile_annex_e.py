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

from pydicom.datadict import dictionary_VM, dictionary_VR

from support.annex_e import (ACTION_FOR_CODE, DEVIATIONS, EDITION, GROUP_RULES,
                             NO_ENTRY, NO_ENTRY_CODES, derive, load_table,
                             render_literal)

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

    # 590 since #556/#557: the 32 concrete `60xx` keys became the two
    # group rules, and 119 D-arm rules moved from EMPTY or REMOVE to
    # REPLACE, which writes the VR's dummy. Literal numbers, checked
    # against the arithmetic by hand, never computed.
    assert len(BASIC_PROFILE) == 590
    assert collections.Counter(rule["action"] for rule in BASIC_PROFILE.values()) == {
        "REMOVE": 412, "EMPTY": 57, "REPLACE": 121}


#: Rows read off PS3.15 2026c Table E.1-1 itself, not off the fixture: one
#: or more of each code the mapping turns into a rule, a sequence of each
#: action, and a `U` row. The histogram test above cannot see a consistent
#: swap of two codes with equal counts in the fixture; these rows can.
#: Checked against the review's independent parse of the standard's HTML
#: (stdlib `html.parser`, no code shared with the fixture's extractor).
#: A table refresh that changes one of these changes this list on purpose.
FROM_THE_STANDARD = {
    # key: (name, Basic Prof. code, rule action or None)
    "0010,1000": ("Other Patient IDs", "X", "REMOVE"),
    "0008,0021": ("Series Date", "X/D", "REPLACE"),
    "0018,1030": ("Protocol Name", "X/D", "REPLACE"),
    "0008,0050": ("Accession Number", "Z", "EMPTY"),
    "0010,0030": ("Patient's Birth Date", "Z", "EMPTY"),
    "0008,0023": ("Content Date", "Z/D", "REPLACE"),
    "0008,1010": ("Station Name", "X/Z/D", "REPLACE"),
    "0040,a075": ("Verifying Observer Name", "D", "REPLACE"),
    # A D-arm sequence keeps its action, a named departure (#557): no
    # dummy item is valid independent of the IOD.
    "0008,0082": ("Institution Code Sequence", "X/Z/D", "EMPTY"),
    # The table's own repeating-group spelling is the rule key (#556).
    "50xx,xxxx": ("Curve Data", "X", "REMOVE"),
    "0040,0275": ("Request Attributes Sequence", "X", "REMOVE"),
    "0008,1110": ("Referenced Study Sequence", "X/Z", "EMPTY"),
    "0020,000d": ("Study Instance UID", "U", None),
}


def test_rows_pinned_from_the_standard_itself():
    """Kills a consistent code swap in the fixture (every X read as Z and
    back, say), which leaves every count above unchanged, and a mapping
    change that flips a whole code."""
    rows = {row["key"]: row for row in load_table()["rows"]}

    for key, (name, code, action) in FROM_THE_STANDARD.items():
        assert rows[key]["name"] == name, key
        assert rows[key]["basic"] == code, (key, rows[key]["basic"])
        if action is None:
            assert key not in BASIC_PROFILE, key
        else:
            assert BASIC_PROFILE[key]["action"] == action, key
    assert {code for _, code, _ in FROM_THE_STANDARD.values()} == (
        set(ACTION_FOR_CODE) | {"U"})


def test_the_literal_is_its_rendering_comments_included():
    """`profiles.py` says to regenerate the literal. A comment written into
    it by hand would be lost the next time someone does, so the block is
    compared with `render_literal()` as text, and its comments live in
    `LITERAL_COMMENTS`.

    Kills: a comment added, edited or dropped in the literal and not in
    `LITERAL_COMMENTS`, and a trailing code comment that disagrees with
    the table."""
    text = (REPO / "isocenter" / "profiles.py").read_text(encoding="utf-8")
    start = text.index("BASIC_PROFILE = {\n")
    end = text.index("\n}\n", start) + len("\n}\n")

    assert text[start:end] == render_literal(load_table())


def test_the_floor_overrides_three_basic_rules_and_adds_none():
    """Patient's Age is a basic rule since 0.9.8, so all three research
    defaults override one and the floor is the profile's size."""
    assert set(RESEARCH_DEFAULTS) <= set(BASIC_PROFILE)
    assert len(FLOOR_POLICY) == len(BASIC_PROFILE) == 590


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

    # A group rule is the other way round: a key the table does not have
    # (#556), so it is added, not departed to, and owns an issue its
    # reason repeats.
    assert GROUP_RULES, "the overlay group rule is gone"
    for key, rule in GROUP_RULES.items():
        assert key not in rows, f"GROUP_RULES names {key}, which is a table row"
        assert rule["action"] in ("REMOVE", "KEEP"), key
        assert rule["name"].strip() and rule["reason"].strip(), key
        assert re.fullmatch(r"#\d+", rule["authority"]), key
        assert rule["authority"] in rule["reason"], key


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


def test_537s_deviation_is_the_patients_name():
    """#537 decided the three owned rules: Study Date follows the table
    (Z, so EMPTY), and Patient's Name departs from it to REPLACE, a dummy
    Z permits. Patient ID (Z/D) was the second departure until #557; its
    code now maps to REPLACE, and REPLACE with no value on Patient ID is
    the keyed pseudonym, which is its D arm. Kills a departure kept on
    Study Date, the name's dropped, and the Patient ID row silently
    becoming something else."""
    assert {key: d["action"] for key, d in DEVIATIONS.items()
            if d["authority"] == "#537"} == {"0010,0010": "REPLACE"}
    assert BASIC_PROFILE["0010,0020"] == {"action": "REPLACE", "name": "Patient ID"}


#: The VRs a value-less REPLACE writes a dummy for (#557). Literal, not
#: read from `config_manager.VR_DUMMY`: the test would otherwise pass any
#: change to the table it is guarding.
DUMMY_VRS = {"AE", "CS", "LO", "LT", "PN", "SH", "ST", "UC", "UT", "UR",
             "DA", "DT", "TM", "AS", "OB", "OW", "OF", "OL", "OD", "OV", "UN"}


def _dictionary(key):
    number = int(key.replace(",", ""), 16)
    return str(dictionary_VR(number)), str(dictionary_VM(number))


def test_no_dummy_lands_on_a_sequence_or_a_uid():
    """Every value-less REPLACE in the literal is on a VR that has a dummy
    (#557). The mapping sends every D arm to REPLACE, so a later table
    giving D to a sequence or a UID row would become a REPLACE the scan
    ignores (SQ) or the loader refuses (UI); this says why, where the
    derivation test would only say the literal differs.

    Kills: a D-arm sequence departure deleted from `DEVIATIONS` (its row
    derives to REPLACE on an SQ)."""
    for key, rule in BASIC_PROFILE.items():
        if rule["action"] != "REPLACE" or "value" in rule:
            continue
        vr, _vm = _dictionary(key)
        assert vr in DUMMY_VRS, (
            f"{key} ({rule['name']}) is a value-less REPLACE on {vr}, which "
            f"has no dummy; give the row a departure or a dummy")


def test_every_d_arm_row_is_single_valued_and_not_numeric():
    """The dummy is one value of a string or binary VR. That is right
    because every row whose code has a D arm is VM 1 or 1-n, and none is
    numeric, AT or a compound VR (measured on the 2026c table). Kills a
    table refresh that breaks either premise without anyone noticing."""
    numeric = {"US", "SS", "UL", "SL", "UV", "SV", "FL", "FD", "DS", "IS", "AT"}
    rows = [row for row in load_table()["rows"]
            if "D" in row["basic"].split("/")]
    assert len(rows) == 130
    for row in rows:
        vr, vm = _dictionary(row["key"])
        assert vm in ("1", "1-n"), (row["key"], vm)
        assert vr not in numeric and " or " not in vr, (row["key"], vr)


def test_the_configuration_page_names_the_fixture_edition():
    """The docs say which edition the profile is. A refresh that changes
    the fixture and not the page is red here.

    Kills: the page naming 2026b (or nothing) while the fixture says 2026c."""
    text = (REPO / "docs" / "configuration.md").read_text(encoding="utf-8")

    named = set(re.findall(r"\b(20\d\d[a-e])\b", text))
    assert named == {EDITION}, (
        f"docs/configuration.md names edition(s) {sorted(named)}; the "
        f"fixture is {EDITION}")

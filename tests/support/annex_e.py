"""PS3.15 Table E.1-1 as data, and the rule that turns it into `BASIC_PROFILE` (#547).

`isocenter/profiles.py` holds the profile as a pasted literal, because
it is registered with the mutation probe as data with zero sites and a
loop over a table there would turn `test_a_zero_sites_reason_still_has_zero_sites`
red. The derivation lives here instead, and
`tests/test_basic_profile_annex_e.py` holds the literal equal to it. So
the literal cannot drift from the table, and every place the profile
departs from the table is written below, once, with its reason.

To refresh the table: replace the fixture with the new edition's rows
(the fixture's header says what each field is), rename the file and
`EDITION`, update the edition named in `docs/configuration.md`, and
regenerate the literal by printing `derive(load_table())`. A test fails
at each step that is skipped.

In `tests/support/` because pytest collects only `test_*.py` at the top
of `tests/` (#347). No `isocenter` import, so the mutation probe charges
coverage to the tests that use this.
"""
import json
import pathlib
import re

EDITION = "2026c"
FIXTURE = (pathlib.Path(__file__).resolve().parent.parent
           / "fixtures" / f"ps3.15-{EDITION}-table-e1-1.json")

#: The Basic Prof. column's codes, and the action each becomes.
#:
#: - `X` removes. `X/D` too: X is the first arm, and a context whose IOD
#:   needs D (Type 1) is not served by REMOVE or EMPTY alike (#557).
#: - Every code with a `Z` arm empties (`Z`, `Z/D`, `X/Z`, `X/Z/D`).
#:   Zero length is valid where the table's X is valid (Type 3) and where
#:   its Z is (Type 2); removal is valid only for Type 3. So EMPTY is right
#:   in every context either arm is, without a per-IOD type table (#558).
#:   Removing was 0.9.7's reading and dropped four Type 2 attributes from
#:   every export.
#: - `D` empties. D asks for a non-zero dummy consistent with the VR, and
#:   Isocenter has no such action yet (#557), so an attribute that is
#:   Type 1 in its IOD is written zero-length. A stated non-conformance.
ACTION_FOR_CODE = {
    "X": "REMOVE",
    "X/D": "REMOVE",
    "Z": "EMPTY",
    "Z/D": "EMPTY",
    "X/Z": "EMPTY",
    "X/Z/D": "EMPTY",
    "D": "EMPTY",
}

#: Codes that give no rule at all.
NO_ENTRY_CODES = {
    "U": "#544: replacing UIDs consistently is its own mechanism, not a tag rule",
    "X/Z/U*": "#544: a sequence of UID references; its contents need UID replacement",
}

#: Rows whose code would give a rule, and which get none.
NO_ENTRY = {
    "gggg,eeee": "private attributes are the `remove_private_tags` sweep, "
                 "on by default, not a rule",
    "006a,0003": "#544: D on a UI, and a dummy UID is UID replacement",
    "0040,a730": "D on a sequence whose identifying contents are rows of "
                 "their own, so recursion cleans it",
    "0070,0001": "D on a sequence whose identifying contents are rows of "
                 "their own, so recursion cleans it",
    "0040,a073": "D on a sequence whose identifying contents are rows of "
                 "their own, so recursion cleans it",
    "0034,0001": "D on a sequence whose identifying contents are rows of "
                 "their own, so recursion cleans it",
}

#: Where the profile departs from the column. `action` None means no rule.
#: `authority` is an issue number, or `path::symbol` naming the code whose
#: behaviour forces the departure; the test checks the symbol is there.
DEVIATIONS = {
    "0010,0010": {
        "action": "REMOVE", "authority": "#537",
        "reason": "Patient's Name is entity-owned: anonymize() replaces it "
                  "whatever the rule says. The table's Z waits on #537"},
    "0010,0020": {
        "action": "REMOVE", "authority": "#537",
        "reason": "Patient ID is entity-owned: anonymize() writes the "
                  "pseudonym whatever the rule says. The table's Z/D waits "
                  "on #537"},
    "0008,0020": {
        "action": "REMOVE", "authority": "#537",
        "reason": "Study Date is entity-owned: anonymize() shifts it whatever "
                  "the rule says. The table's Z waits on #537"},
    "0008,1030": {
        "action": "EMPTY", "authority": "isocenter/io_handlers.py::export_folder_names",
        "reason": "the export directory names read Study Description; zero "
                  "length is valid wherever the table's X is"},
    "0008,103e": {
        "action": "EMPTY", "authority": "isocenter/io_handlers.py::export_folder_names",
        "reason": "the export directory names read Series Description; zero "
                  "length is valid wherever the table's X is"},
    "0040,b020": {
        "action": None, "authority": "isocenter/waveform.py::TAG_ANNOTATION_SEQ",
        "reason": "Waveform Annotation Sequence carries the Murmur annotation "
                  "bridge; its free text (0070,0006) and nested names and "
                  "dates are rows of their own, cleaned by recursion"},
    "0088,0200": {
        "action": None, "authority": "#183",
        "reason": "Icon Image Sequence is dropped whenever pixels are redacted "
                  "(#183); without redaction the full frame carries whatever "
                  "the icon does, and the basic profile does not clean pixels"},
    "50xx,xxxx": {
        "action": None, "authority": "#556",
        "reason": "Curve Data names a whole retired group, which no rule "
                  "key can spell; a repeating-group sweep is #556"},
}

#: Rows naming an element in a repeating group, written as one rule per
#: group because the loader refuses mask keys. The groups are the even
#: groups 6000-601E (PS3.5 7.6).
REPEATING_GROUPS = {"60xx": [f"{group:04x}" for group in range(0x6000, 0x6020, 2)]}


def load_table(path=FIXTURE):
    """The fixture as a dict: `edition`, `source`, `fetched`, `rows`."""
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def rule_name(row):
    """The row's attribute name as a rule carries it: without the table's
    "(see Note N)" cross-references."""
    return re.sub(r"\s*\(see Note \d+\)", "", row["name"])


def derive(table):
    """`BASIC_PROFILE` as the table, the mapping and the departures make it."""
    profile = {}
    for row in table["rows"]:
        key, code = row["key"], row["basic"]
        if key in DEVIATIONS:
            action = DEVIATIONS[key]["action"]
        elif key in NO_ENTRY or code in NO_ENTRY_CODES:
            action = None
        else:
            action = ACTION_FOR_CODE[code]
        if action is None:
            continue
        group, element = key.split(",")
        for concrete in REPEATING_GROUPS.get(group, [group]):
            profile[f"{concrete},{element}"] = {"action": action,
                                                "name": rule_name(row)}
    return profile

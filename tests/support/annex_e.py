"""PS3.15 Table E.1-1 as data, and the rule that turns it into `BASIC_PROFILE` (#547).

`isocenter/profiles.py` holds the profile as a pasted literal, because
it is registered with the mutation probe as data with zero sites and a
loop over a table there would turn `test_a_zero_sites_reason_still_has_zero_sites`
red. The derivation lives here instead, and
`tests/test_basic_profile_annex_e.py` holds the literal equal to it. So
the literal cannot drift from the table, and every place the profile
departs from the table is written below, once, with its reason.

A profile name is pinned to its edition (#714): `basic@2026c` is the rule
table the v1.0.0 tag ships under that name -- this fixture, the mapping
below and the named departures -- and from that tag it never changes
(`tests/test_profile_editions.py` holds its digest). Replacing the
fixture with a later edition's, which is how this file used to say the
table was refreshed, would rewrite what `basic@2026c` means, and is
forbidden. A later edition is added beside it:

1. Vendor `tests/fixtures/ps3.15-<edition>-table-e1-1.json` beside the
   2026c one. Never replace or edit the 2026c file.
2. Give the new edition its own derivation. The 2026c mapping, departures
   and literal comments are frozen with the name, so a rule change for
   the new edition must not reach 2026c: at the first new edition, split
   this module's constants per edition. (Not before: there is one.)
3. Add a second literal (`BASIC_2027A`) to `isocenter/profiles.py` and
   `PRIVACY_PROFILES["basic@2027a"]`. `PROFILE_ALIASES` and `FLOOR_BASE`
   do not move in 1.x.
4. A new name is a new configuration value, so bump
   `config_manager.CONFIG_VERSION` to the next minor, add its row (with
   the new name) to `SCHEMA_BY_VERSION` in
   `tests/test_config_schema_version.py`, and pin the new name's digest.
5. `test_the_configuration_page_names_the_fixture_edition` becomes "the
   page names every shipped edition".

Before the v1.0.0 tag the 2026c table may still change (#544, #557): edit
the mapping or a departure here, regenerate the literal by pasting
`render_literal(load_table())` over it, and update the pinned digests in
`tests/test_profile_editions.py` with a CHANGELOG entry.

The literal's explanatory comments live in `LITERAL_COMMENTS` below and
are emitted by `render_literal`, and the literal is held equal to that
rendering, comments included: a comment written by hand into the literal
would be lost on the next regeneration, so it has to be written here.

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
        "action": "REPLACE", "authority": "#537",
        "reason": "Z permits a dummy; ANONYMIZED keeps every existing "
                  "export's Patient's Name and the lock refusal's default "
                  "(#537)"},
    "0010,0020": {
        "action": "REPLACE", "authority": "#537",
        "reason": "a Patient ID rule may not empty or remove it (#537): the "
                  "keyed pseudonym is the D arm's dummy"},
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

#: Comment lines `render_literal` writes above a rule in the literal, by
#: rule key: history and cross-references a reader of `profiles.py` needs
#: at the entry. A departure's reason belongs in `DEVIATIONS`.
LITERAL_COMMENTS = {
    "0008,0020": [
        "Z in the table. Owned by the Study, and since #537 this rule governs",
        "the study's own date: `basic` exports it zero-length. The floor",
        "JITTERs it (RESEARCH_DEFAULTS).",
    ],
    "0008,002a": [
        "DT-valued twin of Acquisition Date: until #38 raw acquisition",
        "timing survived a full anonymize() pass while the plain date was",
        "stripped.",
    ],
    "0008,0030": [
        "Z, and Type 2 in General Study (PS3.3 C.7.2.1), so the element",
        "stays present and empty. REMOVE here plus a validator that called",
        "it Type 1 meant the documented Quick Start exported nothing on any",
        "CT file (#495).",
    ],
    "0008,1010": [
        "Absent until #495, so CT_small's `CT01_OC0` survived even the",
        "documented path.",
    ],
    "0008,1030": [
        "X in the table, EMPTY here: the export directory names read it",
        "(`io_handlers.export_folder_names`), and zero length is valid",
        "wherever X is. The same for Series Description below.",
    ],
    "0008,2111": [
        "Isocenter's own redaction note here is exempt, by exact value, in",
        "`PhiInspector._scan_instance`: safe export would otherwise skip",
        "every redacted instance.",
    ],
    "0010,0010": [
        "Z in the table, REPLACE here: ANONYMIZED is a dummy Z permits. Owned",
        "by the Patient, as Patient ID below (Z/D, the keyed pseudonym): #537.",
    ],
    "0070,0006": [
        "Free-text annotation commentary. Reaches annotations.json `note`",
        "when a caller opts in via include_annotation_text; remediated here",
        "so that opting in still does not surface raw text.",
    ],
    "6000,3000": [
        "Repeating group 60xx: one rule per even group 6000-601E, because",
        "the loader refuses mask keys. Removing Overlay Data leaves the",
        "rest of the Overlay Plane module (#556).",
    ],
}


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


def render_literal(table):
    """The `BASIC_PROFILE = {...}` block of `isocenter/profiles.py`, as
    text: `derive(table)` in key order, each entry's table code as a
    trailing comment, and `LITERAL_COMMENTS` above the entries they name.
    Paste it over the block to regenerate; the test compares it with the
    file character for character."""
    rows = {row["key"]: row for row in table["rows"]}
    lines = ["BASIC_PROFILE = {"]
    for key, rule in sorted(derive(table).items()):
        for comment in LITERAL_COMMENTS.get(key, []):
            lines.append(f"    # {comment}")
        group, element = key.split(",")
        row = rows.get(key) or next(
            rows[f"{mask},{element}"] for mask, groups in REPEATING_GROUPS.items()
            if group in groups)
        name = json.dumps(rule["name"], ensure_ascii=False)
        lines.append(f'    "{key}": {{"action": "{rule["action"]}", '
                     f'"name": {name}}},  # {row["basic"]}')
    lines.append("}")
    return "\n".join(lines) + "\n"

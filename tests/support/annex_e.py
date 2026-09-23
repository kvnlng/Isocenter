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
#: - `X` removes.
#: - `Z` and `X/Z` empty. Zero length is valid where the table's X is
#:   valid (Type 3) and where its Z is (Type 2); removal is valid only for
#:   Type 3. So EMPTY is right in every context either arm is, without a
#:   per-IOD type table (#558). Removing was 0.9.7's reading and dropped
#:   four Type 2 attributes from every export.
#: - `D` and every code with a D arm (`X/D`, `Z/D`, `X/Z/D`) REPLACE, and
#:   a value-less REPLACE writes the dummy of the tag's VR
#:   (`config_manager.VR_DUMMY`, #557). PS3.15 Table E.1-1a defines D as
#:   "replace with a non-zero length value that may be a dummy value and
#:   consistent with the VR", and Z as "a zero length value, or a non-zero
#:   length value that may be a dummy value and consistent with the VR",
#:   so the dummy is what the code asks for where it resolves to D, and
#:   is permitted where it resolves to Z. Where it resolves to X (the
#:   attribute is Type 3 in its IOD) the code removes it and Isocenter
#:   writes the dummy instead: a named departure from the X arm. E.1.1
#:   permits it as protection ("either be removed ... or have its value
#:   replaced by a different 'replacement value' that does not allow
#:   identification of the patient"); resolving each arm per IOD is #558.
#:   Until #557 `D` emptied and `X/D` removed, so an attribute Type 1 in
#:   its IOD was written zero-length or dropped. Sequences keep their
#:   actions (`DEVIATIONS`): no dummy item is valid independent of the IOD.
#: - `U` REPLACEs, with no value, which on a UI is the keyed UID
#:   replacement (#544): `U` is "replace with a non-zero length UID that
#:   is internally consistent within a set of Instances", and the
#:   replacement is a function of the value alone, so every reference to
#:   one UID gets the same one. Every `U` row is a UI. Until #544 `U` gave
#:   no rule and every UID was exported as ingested.
ACTION_FOR_CODE = {
    "U": "REPLACE",
    "X": "REMOVE",
    "X/D": "REPLACE",
    "Z": "EMPTY",
    "Z/D": "REPLACE",
    "X/Z": "EMPTY",
    "X/Z/D": "REPLACE",
    "D": "REPLACE",
}

#: Codes that give no rule at all.
NO_ENTRY_CODES = {
    "X/Z/U*": "#544: the sequence is kept; the UID references inside it "
              "(0008,1155, ...) are U rows of their own and are replaced "
              "wherever they sit, which keeps the references resolving. X/Z "
              "would drop them",
}

#: Rows whose code would give a rule, and which get none.
NO_ENTRY = {
    "gggg,eeee": "private attributes are the `remove_private_tags` sweep, "
                 "on by default, not a rule",
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
    # The four D-arm sequences keep the action they had (#557). PS3.15
    # E.1-1a applies a sequence's code "to the Sequence and all of its
    # contents", so D there is a non-empty sequence of valid items, and
    # what makes an item valid is the sequence's item macro in its IOD
    # (#558). Without these the mapping gives them REPLACE, which has no
    # meaning on a sequence.
    "0008,0082": {
        "action": "EMPTY", "authority": "#557",
        "reason": "a D-arm sequence: a dummy item depends on the IOD (#557), "
                  "and its Code Meanings name the institution"},
    "0008,1072": {
        "action": "REMOVE", "authority": "#557",
        "reason": "a D-arm sequence: a dummy item depends on the IOD (#557), "
                  "and its items identify the operator"},
    "0008,1111": {
        "action": "EMPTY", "authority": "#557",
        "reason": "a D-arm sequence: a dummy item depends on the IOD (#557), "
                  "and its items are SOP references, which are UID "
                  "replacement's (#544)"},
    "0040,1101": {
        "action": "EMPTY", "authority": "#557",
        "reason": "a D-arm sequence: a dummy item depends on the IOD (#557), "
                  "and its items are person identification codes"},
    # Folded into the `60xx,xxxx` group rule (#556). With them beside it,
    # `"60xx,xxxx": {action: KEEP}` would still remove Overlay Data
    # through the more specific key and leave the invalid module the
    # group rule exists to prevent.
    "60xx,3000": {
        "action": None, "authority": "#556",
        "reason": "subsumed by the 60xx,xxxx group rule: one rule, so that "
                  "a KEEP of the group keeps a valid module (#556)"},
    "60xx,4000": {
        "action": None, "authority": "#556",
        "reason": "subsumed by the 60xx,xxxx group rule: one rule, so that "
                  "a KEEP of the group keeps a valid module (#556)"},
}

#: Rules on keys the table does not have (#556). `60xx,xxxx` is the whole
#: Overlay Plane module, in every even group 6000-601E (PS3.5 7.6). The
#: table names only Overlay Data and Overlay Comments, and removing Overlay
#: Data alone leaves a module without its Type 1 element (PS3.3 C.9-2),
#: while Overlay Description and Overlay Label, free text the table does
#: not list, reach the export holding a name.
GROUP_RULES = {
    "60xx,xxxx": {
        "action": "REMOVE", "name": "Overlay (whole group)", "authority": "#556",
        "reason": "PS3.15 E.1.1: \"If non-pixel data graphics or overlays "
                  "contain identification, the de-identifier is required to "
                  "remove them\"; and Overlay Data is Type 1 in the Overlay "
                  "Plane module (PS3.3 C.9-2), so removing it alone leaves an "
                  "invalid module (#556)"},
}

#: Comment lines `render_literal` writes above a rule in the literal, by
#: rule key: history and cross-references a reader of `profiles.py` needs
#: at the entry. A departure's reason belongs in `DEVIATIONS`.
LITERAL_COMMENTS = {
    "0008,0018": [
        "U in the table, as every UI row below is. REPLACE with no value on a",
        "UI is the keyed UID (#544): one value, one replacement, wherever it",
        "sits; a UID this project minted is left as it is.",
    ],
    "006a,0003": [
        "D on a UI: the keyed UID is a non-zero value consistent with the VR,",
        "which is what D asks for (#544).",
    ],
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
    "50xx,xxxx": [
        "Repeating-group key (#556): every element of every even group",
        "5000-501E, the retired Curve module. Until #556 no rule key could",
        "spell it, and every curve element survived every profile.",
    ],
    "60xx,xxxx": [
        "Not a table row: the whole Overlay Plane module in every even group",
        "6000-601E (#556). The table's Overlay Data and Overlay Comments rows",
        "are folded into it, so a KEEP of the group keeps a valid module; a",
        "more specific key (`60xx,0022`, `6002,0022`) still wins over it.",
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
        # A repeating-group row keeps its mask key (`50xx,xxxx`): the
        # loader reads the table's own spelling since #556.
        profile[key] = {"action": action, "name": rule_name(row)}
    for key, rule in GROUP_RULES.items():
        profile[key] = {"action": rule["action"], "name": rule["name"]}
    return profile


def render_literal(table):
    """The `BASIC_PROFILE = {...}` block of `isocenter/profiles.py`, as
    text: `derive(table)` in key order, each entry's table code as a
    trailing comment (`group rule (#556)` for a `GROUP_RULES` key, which
    has no row), and `LITERAL_COMMENTS` above the entries they name.
    Paste it over the block to regenerate; the test compares it with the
    file character for character."""
    rows = {row["key"]: row for row in table["rows"]}
    lines = ["BASIC_PROFILE = {"]
    for key, rule in sorted(derive(table).items()):
        for comment in LITERAL_COMMENTS.get(key, []):
            lines.append(f"    # {comment}")
        code = (rows[key]["basic"] if key in rows
                else f"group rule ({GROUP_RULES[key]['authority']})")
        name = json.dumps(rule["name"], ensure_ascii=False)
        lines.append(f'    "{key}": {{"action": "{rule["action"]}", '
                     f'"name": {name}}},  # {code}')
    lines.append("}")
    return "\n".join(lines) + "\n"

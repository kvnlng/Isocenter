"""PS3.15's Retain UIDs Option, as a `phi_tags` block (#544).

`KEEP` on every rule `basic@2026c` gives a value-less `REPLACE` on a UI
attribute: the table's `U` rows and Annotation Group UID (`D` on a UI).
Read from the vendored table, never from `isocenter.profiles`, so a test
that uses it names no probe target.

For a test that stands in for an export written **before 1.0**, which
kept every UID: keeping them is what makes the stand-in faithful, not a
way around the replacement. A test of the 1.0 pipeline asserts the
replacements instead.
"""
from support.annex_e import load_table

KEEP_UIDS = {row["key"]: {"name": row["name"], "action": "KEEP"}
             for row in load_table()["rows"]
             if row["basic"] == "U" or row["key"] == "006a,0003"}


def keep_uids(session):
    """Give `session`'s configuration `KEEP` on every UID row."""
    for tag in KEEP_UIDS:
        session.configuration.set_phi_tag(tag, "KEEP")

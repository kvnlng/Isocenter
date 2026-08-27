"""CLAUDE.md must not name a persistence API that does not exist.

Its dirty-tracking paragraph documented `_mod_count`, `_saved_mod_count`,
`_dirty`, `mark_saved(version)` and `mark_clean()`. None of the five had
ever existed under those names -- the real surface is `_revision`,
`_persisted_revision`, `has_unsaved_changes`, `mark_modified()` and
`mark_persisted()`. It also called `mark_clean()` and "the `_dirty`
setter" legacy escape hatches to prefer `mark_saved` over, where
`TrackedEntity` says outright that there is deliberately no setter,
because "declare this saved" is the operation that let a rolled-back save
leave instances claiming they had been written.

That is worse than a stale sentence. CLAUDE.md is loaded into every
session, so it is the first thing an agent reads about persistence, and
it was recommending an escape hatch that had been removed on purpose in
the subsystem whose failure mode is PHI staying in the database.

This checks only the identifiers CLAUDE.md spells with backticks in the
"Object graph and dirty tracking" section, against the real attributes of
`TrackedEntity`. Prose is not checked and neither is the rest of the
file: the aim is that a rename cannot leave this paragraph describing an
API nobody can call.
"""
import pathlib
import re

from isocenter.entities import PhiStatus, TrackedEntity

ROOT = pathlib.Path(__file__).resolve().parent.parent
SECTION = "### Object graph and dirty tracking"

# Backticked names in that section which are not TrackedEntity's surface:
# tag literals, sibling classes, and the loader/accessor pair the section
# also covers. Anything else must resolve.
NOT_TRACKED_ENTITY = {
    "DicomStore", "store.py", "entities.py", "DicomItem", "attributes",
    "sequences", "Patient", "Study", "Series", "Instance", "TrackedEntity",
    "SidecarPixelLoader", "SidecarWaveformLoader", "get_pixel_data()",
    "unload_pixel_data()", "set_attr", "add_sequence_item", "max",
    "remediation.py",
}


def _section_text():
    body = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    start = body.index(SECTION)
    nxt = body.index("\n### ", start + len(SECTION))
    return body[start:nxt]


def test_every_persistence_api_claude_md_names_exists():
    text = _section_text()

    # `name`, `name()`, `name(args)` -- keep the bare identifier.
    candidates = {
        re.sub(r"\(.*\)$", "", token)
        for token in re.findall(r"`([A-Za-z_][A-Za-z0-9_]*(?:\([^`]*\))?)`", text)
    }
    candidates -= {re.sub(r"\(.*\)$", "", n) for n in NOT_TRACKED_ENTITY}
    candidates -= {n for n in NOT_TRACKED_ENTITY}

    # The section covers both halves of the vocabulary, so both count:
    # persistence state on `TrackedEntity`, and the PHI status enum it
    # is deliberately kept separate from.
    real = (set(dir(TrackedEntity))
            | set(getattr(TrackedEntity, "__slots__", ()))
            | {member.name for member in PhiStatus})
    missing = sorted(name for name in candidates if name not in real)

    assert not missing, (
        "CLAUDE.md's dirty-tracking section names these, but they are not "
        f"on TrackedEntity: {missing}.\n"
        f"Its actual surface is: {sorted(n for n in real if not n.startswith('__'))}\n"
        "Update CLAUDE.md -- an agent reads it before it reads the code.")


def test_the_section_still_covers_the_core_api():
    """Guards the guard: a section trimmed to nothing would pass above.

    If someone rewrites the paragraph without these, the check overhead
    is buying nothing and the omission is the finding.
    """
    text = _section_text()
    for name in ("_revision", "_persisted_revision", "has_unsaved_changes",
                 "mark_modified", "mark_persisted"):
        assert f"`{name}" in text, (
            f"CLAUDE.md's dirty-tracking section no longer mentions `{name}`, "
            "which is core to how persistence decides what to write.")

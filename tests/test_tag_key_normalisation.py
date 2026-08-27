"""Tag keys have one canonical form, enforced where they enter (#51).

DICOM tags are `"gggg,eeee"` strings throughout Isocenter. Ingestion
lowercases them -- `io_handlers.populate_attrs` builds
`f"{elem.tag.group:04x},{elem.tag.element:04x}"` -- but hand-authored
keys in profiles, configs, scripts and fixtures are frequently written
with uppercase hex letters (`0008,103E`, `0040,A160`).

A mismatched key yields "absent", never an error, so the failure looks
like ordinary missing data. Two of the three encounters recorded on #51
were silent PHI-relevant defects: a Basic-profile entry for Series
Description that never matched and so was never remediated (#41), and a
folder-naming helper that silently dropped descriptions (#40).

`set_attr` is the choke point for hand-authored keys entering the object
graph, so it normalises. These tests pin that, and -- separately -- pin
that normalising did not disturb the revision counter persistence reads.
"""
import ast
import pathlib
import re

import pytest

from isocenter.builders import PatientBuilder
from isocenter.entities import Instance


def _instance():
    return Instance("1.2.3.4.5", "1.2.840.10008.5.1.4.1.1.2", 1)


# --- the normalisation itself ----------------------------------------

def test_an_uppercase_tag_is_stored_under_the_canonical_spelling():
    inst = _instance()

    inst.set_attr("0008,103E", "Axial 3mm")

    assert inst.attributes["0008,103e"] == "Axial 3mm"
    assert "0008,103E" not in inst.attributes


def test_the_two_spellings_are_one_attribute_not_two():
    """The defect, stated positively.

    Before normalisation these were separate keys, so a lookup written
    in one casing missed a value written in the other -- and both sat in
    `attributes` at once, so nothing looked wrong.
    """
    inst = _instance()
    before = set(inst.attributes)

    inst.set_attr("0008,103E", "first")
    inst.set_attr("0008,103e", "second")

    assert set(inst.attributes) - before == {"0008,103e"}
    assert inst.attributes["0008,103e"] == "second"


def test_a_sequence_tag_is_normalised_too():
    """`0040,A730` (Content Sequence) is the tag SR nesting is built on,
    and `scripts/generate_test_dataset.py` writes it uppercase."""
    inst, item = _instance(), _instance()

    inst.add_sequence_item("0040,A730", item)

    assert "0040,a730" in inst.sequences
    assert "0040,A730" not in inst.sequences


def test_the_builder_normalises_because_it_goes_through_set_attr():
    builder = PatientBuilder("PAT1", "DOE^JOHN")
    inst = (builder.add_study("1.2.3", "20230101")
            .add_series("1.2.4", "CT", 1)
            .add_instance("1.2.5", "1.2.840.10008.5.1.4.1.1.2", 1))

    inst.set_attribute("0040,A160", "History: ...")

    assert "0040,a160" in inst.instance.attributes


def test_a_lowercase_tag_is_left_exactly_as_it_was():
    """Normalisation must not become a general mangler: only case."""
    inst = _instance()
    before = set(inst.attributes)

    inst.set_attr("0008,103e", "Axial 3mm")

    assert set(inst.attributes) - before == {"0008,103e"}


# --- what normalising must not disturb -------------------------------
#
# `set_attr` sits on the revision counter persistence reads
# (`has_unsaved_changes` is `_revision > _persisted_revision`). Editing
# it is the one change in #51 that can break *saves* rather than merely
# lookups, so the counter gets its own assertions rather than relying on
# the suite to notice.

def test_setting_an_uppercase_tag_still_marks_the_item_modified():
    inst = _instance()
    inst.mark_persisted()
    assert not inst.has_unsaved_changes

    inst.set_attr("0008,103E", "Axial 3mm")

    assert inst.has_unsaved_changes, (
        "an edit spelled with an uppercase hex letter did not mark the "
        "item dirty, so it would never be written")


def test_normalising_to_an_existing_key_still_counts_as_an_edit():
    """The collapse case: the key already exists in the other casing.

    A dict write that happens to hit an existing key is still a change
    of value, and must still bump the revision -- otherwise the edit is
    made in memory and silently never persisted.
    """
    inst = _instance()
    inst.set_attr("0008,103e", "first")
    inst.mark_persisted()
    assert not inst.has_unsaved_changes

    inst.set_attr("0008,103E", "second")

    assert inst.has_unsaved_changes
    assert inst.attributes["0008,103e"] == "second"


def test_a_sequence_added_under_an_uppercase_tag_still_marks_modified():
    inst, item = _instance(), _instance()
    inst.mark_persisted()

    inst.add_sequence_item("0040,A730", item)

    assert inst.has_unsaved_changes


# --- the shipped sources must not rely on the normalisation ----------

SOURCE_ROOT = pathlib.Path(__file__).resolve().parent.parent / "isocenter"
TAG_LITERAL = re.compile(r'"([0-9a-fA-F]{4},[0-9a-fA-F]{4})"')


def test_no_shipped_source_writes_a_tag_literal_in_uppercase():
    """Normalisation makes casing safe; consistency keeps it obvious.

    `session.py` stamped `"0020,000D"`, `"0020,000E"` and `"0008,103E"`
    onto exported instances. Those were safe only by coincidence --
    their consumer, `DicomExporter._merge`, parses tags with
    `int(x, 16)`, which happens to be case-insensitive. Nothing enforced
    that, and #51 records it as a near-miss waiting for a consumer that
    compares strings.

    One spelling in the sources means a reader never has to know which
    lookups are case-insensitive and which are not.
    """
    offenders = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))

        # Prose is exempt, and must be: the comment in `privacy.py`'s
        # `_normalize_tag_keys` and the docstring on
        # `_get_attr_case_insensitive` both quote "0008,103E" precisely
        # to name the spelling that used to break things. Lowercasing
        # those would delete the explanation. Comments never reach the
        # AST; docstrings do, so they are collected and skipped.
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                body = getattr(node, "body", None)
                if (body and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    docstrings.add(id(body[0].value))

        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and id(node) not in docstrings):
                for tag in TAG_LITERAL.findall(f'"{node.value}"'):
                    if tag != tag.lower():
                        offenders.append(
                            f"{path.relative_to(SOURCE_ROOT.parent)}"
                            f":{node.lineno}: {tag}")

    assert not offenders, (
        "tag literals spelled with uppercase hex letters:\n  "
        + "\n  ".join(offenders))

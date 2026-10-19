"""The hooks in `docs/conf.py` and `docs/_ext/slugs.py`, run without Sphinx.

The `tests` environment has no Sphinx and no docutils, and a test that
skips when they are absent reads as a pass (`tests/test_doc_anchors.py`'s
argument). So conf.py is loaded as a plain module (its top level imports
neither), and each hook is called with the objects Sphinx would hand it:
real package classes for the autodoc hooks, and for `_slug_first` a
stand-in `docutils.nodes` whose only use is the class `findall` is asked
for. What a hook does inside a real build is the docs build's to show
(`sphinx-build -W`, and the review's scans of the built pages); what each
hook decides, and that `setup()` connects it, is pinned here.

The `TARGETS` rows for `isocenter/entities.py` and `isocenter/session.py`
list this file: the skip hook is asked about the first module's real
members, and the display name is checked against the second's class.
"""
import dataclasses
import importlib
import importlib.util
import pathlib
import re
import sys
import types
from typing import Dict, List, Optional, Tuple

import pytest

from isocenter import entities

REPO = pathlib.Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"


def _load(path, name):
    """Execute `path` as module `name`, leaving `sys.path` as it was
    (conf.py puts the checkout and `docs/_ext` on it for Sphinx)."""
    saved = list(sys.path)
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = saved
    return module


CONF = _load(DOCS / "conf.py", "_isocenter_docs_conf")
SLUGS = _load(DOCS / "_ext" / "slugs.py", "_isocenter_docs_slugs")


# --- The slug ------------------------------------------------------------------

@pytest.mark.parametrize("heading, slug", [
    ("Frozen at 1.0", "frozen-at-10"),
    ("What it refuses to do", "what-it-refuses-to-do"),
    ("ISOCENTER_MAX_TASKS_PER_CHILD", "isocenter_max_tasks_per_child"),
    ("Reversible anonymization: keep a way back",
     "reversible-anonymization-keep-a-way-back"),
])
def test_the_slug_is_python_markdowns(heading, slug):
    """Separator `-`, lower case, punctuation dropped, `_` kept: the ids
    the MkDocs site published, which every deep link names."""
    assert SLUGS.pm_slug(heading) == slug


def test_conf_names_the_slug_function_by_its_import_path():
    assert CONF.myst_heading_slug_func == "slugs.pm_slug"
    assert (DOCS / "_ext" / "slugs.py").is_file()
    assert CONF.myst_heading_anchors == 6, (
        "MyST records a section's slug only for headings at or above "
        "myst_heading_anchors; every page has headings to level 4")


# --- _slug_first ---------------------------------------------------------------

def _section(slug, *ids):
    return {"slug": slug, "ids": list(ids)}


def test_the_slug_goes_first_and_is_not_repeated():
    sections = [_section("frozen-at-10", "frozen-at-1-0"),
                _section("verify", "id1", "verify"),
                _section(None, "only"),
                _section("kept", "kept")]
    CONF._put_slugs_first(sections)
    assert [s["ids"] for s in sections] == [
        ["frozen-at-10", "frozen-at-1-0"], ["verify", "id1"], ["only"],
        ["kept"]]


def test_slug_first_reads_the_doctrees_sections(monkeypatch):
    """`_slug_first` asks the doctree for `docutils.nodes.section` and
    rewrites what it gets. docutils is not installed here, so a stand-in
    module provides the class; the hook must ask for exactly it."""
    marker = type("section", (), {})
    nodes = types.ModuleType("docutils.nodes")
    nodes.section = marker
    docutils = types.ModuleType("docutils")
    docutils.nodes = nodes
    monkeypatch.setitem(sys.modules, "docutils", docutils)
    monkeypatch.setitem(sys.modules, "docutils.nodes", nodes)
    asked = []
    found = [_section("frozen-at-10", "frozen-at-1-0")]

    class Tree:
        def findall(self, cls):
            asked.append(cls)
            return iter(found)

    CONF._slug_first(None, Tree())
    assert asked == [marker]
    assert found[0]["ids"] == ["frozen-at-10", "frozen-at-1-0"]


def test_setup_connects_every_hook():
    connected = []

    class App:
        def connect(self, event, handler, priority=500):
            connected.append((event, handler, priority))

    CONF.setup(App())
    assert sorted(connected, key=lambda c: (c[0], c[2])) == [
        ("autodoc-process-signature", CONF._dataclass_signature, 500),
        ("autodoc-skip-member", CONF._skip_entity_member, 500),
        # Below 500: Sphinx's table-of-contents collector reads each
        # signature's `_toc_name` at doctree-read, priority 500, once.
        ("doctree-read", CONF._present_objects, 400),
        ("doctree-read", CONF._slug_first, 500),
    ]


# --- How API objects are presented (review of #1156, owner ruling S10) ------------

def test_on_this_page_lists_members_by_their_own_names():
    """The MkDocs site's table of contents listed `ingest`, `save`, ...
    under each class's heading. Sphinx adds them only with
    `toc_object_entries`, names them `ingest()` with `hide` (the class
    prefix otherwise), and pydata opens them only at `show_toc_level` 2."""
    assert CONF.toc_object_entries is True
    assert CONF.toc_object_entries_show_parents == "hide"
    assert CONF.html_theme_options.get("show_toc_level") == 2
    assert CONF._member_toc_name("ingest()") == "ingest"
    assert CONF._member_toc_name("store") == "store"
    assert CONF._member_toc_name("") == ""


def test_session_is_presented_as_session_under_dicomsessions_id():
    """The class every page teaches as `Session` keeps its documented id,
    `isocenter.session.DicomSession`, which deep links name."""
    from isocenter import Session
    from isocenter.session import DicomSession

    assert Session is DicomSession
    assert CONF._DISPLAY_NAMES == {"isocenter.session.DicomSession": "Session"}
    assert CONF._shown_name(["isocenter.session.DicomSession"],
                            "DicomSession") == "Session"
    assert CONF._shown_name(["pkg.mod.ExportSummary"],
                            "ExportSummary") == "ExportSummary"
    assert CONF._shown_name([], "") == ""


@pytest.mark.parametrize("children, parts", [
    # `store (DicomStore) – The object graph.`
    ([("literal_strong", "store"), ("Text", " ("), ("pending_xref", "DicomStore"),
      ("Text", ")"), ("Text", " \u2013 "), ("Text", "The object graph.")],
     ("store", slice(2, 3), slice(5, 6))),
    # A type of several nodes: `Optional[str]`.
    ([("literal_strong", "path"), ("Text", " ("), ("pending_xref", "Optional"),
      ("literal_emphasis", "["), ("pending_xref", "str"), ("literal_emphasis", "]"),
      ("Text", ")"), ("Text", " \u2013 "), ("Text", "Where."), ("literal", "x")],
     ("path", slice(2, 6), slice(8, 10))),
    # No type.
    ([("literal_strong", "count"), ("Text", " \u2013 "), ("Text", "How many.")],
     ("count", slice(1, 1), slice(2, 3))),
])
def test_an_attribute_item_splits_into_name_type_and_description(children, parts):
    assert CONF._attribute_parts(children) == parts


@pytest.mark.parametrize("children", [
    [],
    [("Text", "store"), ("Text", " \u2013 "), ("Text", "x")],
    [("literal_strong", "store"), ("Text", " ("), ("pending_xref", "T")],
    [("literal_strong", "store"), ("Text", " ("), ("pending_xref", "T"),
     ("Text", ")"), ("Text", "x")],
    [("literal_strong", "store")],
])
def test_any_other_item_shape_is_left_as_the_list_it_was(children):
    """An item the split does not recognise keeps napoleon's rendering,
    rather than becoming a table row that misplaces its text."""
    assert CONF._attribute_parts(children) is None


# --- The entity filters --------------------------------------------------------

def _skips(owner, name):
    return CONF._skip_entity_member(None, "class", name,
                                    getattr(owner, name), False, {})


@pytest.mark.parametrize("owner, name", [
    (entities.Patient, "mark_subtree_persisted"),       # ^mark_ alone
    (entities.Study, "record_date_shift"),               # ^record_
    (entities.Study, "date_shift_vouches_for"),          # _vouches_for$
    (entities.Instance, "record_remediation"),           # Instance in scope
    (entities.Instance, "identity_token_is_this_stores"),
    (entities.DicomItem, "clear_sequence_items"),
    (entities.DicomItem, "mark_modified"),
])
def test_an_entity_classs_own_filtered_member_is_skipped(owner, name):
    assert name in vars(owner), f"{owner.__name__}.{name} is inherited"
    assert _skips(owner, name) is True


@pytest.mark.parametrize("owner, name", [
    # Rendered in api/entities.md's tier-2 section: the filters were never
    # on TrackedEntity, and a global hook hid these (the prototype's bug).
    (entities.TrackedEntity, "mark_modified"),
    (entities.TrackedEntity, "mark_persisted"),
    (entities.TrackedEntity, "record_phi_status"),
    # A public member of an entity class the filters do not name.
    (entities.Instance, "set_pixel_data"),
    (entities.DicomItem, "set_attr"),
])
def test_everything_else_is_left_to_autodoc(owner, name):
    assert _skips(owner, name) is None


def test_the_hook_speaks_only_for_class_members():
    method = entities.DicomItem.mark_modified
    assert CONF._skip_entity_member(None, "module", "mark_modified", method,
                                    False, {}) is None


# --- Dataclass signatures ------------------------------------------------------

_AUTOCLASS = re.compile(r"^\.\.\s+autoclass::\s+(\S+)\s*$", re.MULTILINE)


def _rendered_dataclasses():
    """Every dataclass an API page names in a top-level `.. autoclass::`."""
    found = []
    for page in sorted((DOCS / "api").glob("*.md")):
        for target in _AUTOCLASS.findall(page.read_text(encoding="utf-8")):
            module, _, name = target.rpartition(".")
            cls = getattr(importlib.import_module(module), name)
            if dataclasses.is_dataclass(cls):
                found.append(cls)
    return found


def _shown(cls):
    return [(p.name, repr(p.default)) for p in CONF._dataclass_parameters(cls)]


def test_no_rendered_dataclass_shows_a_factory_or_a_private_field():
    """`<factory>` is not Python, so Sphinx splits such a signature on
    commas and cuts a nested annotation in two, silently (review of C2:
    four classes). Every rendered dataclass goes through the hook."""
    classes = _rendered_dataclasses()
    assert len(classes) >= 12, classes
    for cls in classes:
        shown = _shown(cls)
        assert not any("<factory>" in default for _, default in shown), (cls, shown)
        assert not any(name.startswith("_") for name, _ in shown), (cls, shown)
    names = {cls.__name__ for cls in classes}
    assert {"IsocenterConfiguration", "IngestSummary", "ExportSummary",
            "PhiRemediation", "Instance"} <= names, names


def test_the_parameters_on_a_fixture():
    @dataclasses.dataclass
    class Shape:
        name: str
        failures: List[Tuple[str, str]] = dataclasses.field(default_factory=list)
        meta: Dict[str, int] = dataclasses.field(default_factory=lambda: {"a": 1})
        _loader: Optional[int] = None

    assert _shown(Shape) == [("name", "<class 'inspect._empty'>"),
                             ("failures", "list()"), ("meta", "...")]


def test_the_signature_hook_leaves_other_objects_alone():
    for what, obj in (("method", entities.Instance.set_attr),
                      ("class", entities.PhiStatus),
                      ("function", entities.iter_item_tree)):
        assert CONF._dataclass_signature(None, what, "x", obj, {}, "()", "") is None
    assert CONF._dataclass_signature(None, "class", "x", entities.Patient, {},
                                     None, "") is None

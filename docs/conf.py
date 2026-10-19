"""Sphinx configuration for the documentation site (docs/sphinx).

Built with `sphinx-build -W --keep-going -b dirhtml docs <out>`, from
the `docs` extra in setup.py. The output goes outside the checkout: a
build in the repository root is a stray the test suite's root guard
reports. `tests/test_the_docs_are_myst.py` and `tests/test_doc_anchors.py`
read this file as text and never import it, because the `tests`
environment has no Sphinx; keep the settings they read as plain literal
assignments.
"""
import dataclasses
import inspect
import os
import pathlib
import re
import sys

DOCS = pathlib.Path(__file__).resolve().parent
REPO = DOCS.parent

# The package is documented from this checkout, never from whatever
# `isocenter` the build environment happens to have installed.
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(DOCS / "_ext"))

# Read, not imported: `import isocenter._version` runs the package's
# `__init__`, which imports pydicom and numpy before autodoc needs them.
release = re.search(
    r'^__version__ = "([^"]+)"$',
    (REPO / "isocenter" / "_version.py").read_text(encoding="utf-8"),
    re.MULTILINE).group(1)
version = release

project = "Isocenter"
author = "Kevin Long"
copyright = "2026 Kevin Long"

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx_design",
    "sphinxcontrib.mermaid",
    "sphinx_copybutton",
]

source_suffix = {".md": "markdown"}
exclude_patterns = ["superpowers/**", "_build/**", "_ext/**"]

# index.md starts at `##`: the home page's hero, with its H1, is
# docs/_templates/layout.html's, not a heading of the page; the page's
# title is its first section.
suppress_warnings = ["myst.header"]

# --- MyST ------------------------------------------------------------------

myst_enable_extensions = ["colon_fence", "attrs_inline", "attrs_block"]

# Heading ids. MyST's own slug differs from Python-Markdown's, the
# renderer the MkDocs site used, on 93 of its 269 heading ids
# (`frozen-at-10` against `frozen-at-1-0`), and docutils assigns a third
# spelling. Every published deep link names the MkDocs id, so the site
# keeps it: MyST slugs with Python-Markdown's own `slugify`
# (docs/_ext/slugs.py), and `_slug_first` below makes that slug each
# section's first HTML id. The #152 lesson: one slug function for the
# site and for tests/test_doc_anchors.py, which grades every `#fragment`
# link against Python-Markdown's ids. Named by import path, because a
# function object in conf.py does not pickle into Sphinx's environment.
# MyST records a section's slug only for headings at or above this level,
# so without it `_slug_first` has nothing to put first.
myst_heading_anchors = 6
myst_heading_slug_func = "slugs.pm_slug"

# --- autodoc and napoleon --------------------------------------------------

# The docstrings were written for griffe, in Markdown's backticks. Without
# this every single-backtick span renders as an italic `<cite>`, silently
# (1,036 of them on the API pages when measured).
default_role = "literal"
add_module_names = False
autodoc_member_order = "bysource"
napoleon_google_docstring = True
napoleon_numpy_docstring = False
# An `Attributes:` section becomes fields on its class rather than a
# second `py:attribute` beside the one `:members:` documents, which Sphinx
# reports as a duplicate object description. `_present_objects` below
# turns those fields into the Name/Type/Description table the MkDocs site
# showed, with no ids added or lost.
napoleon_use_ivar = True

# "On this page" lists each documented member under its heading, by its
# own name (`ingest`, not `DicomSession.ingest()`), as the MkDocs site's
# table of contents did. `_present_objects` drops the `()`, and the class
# entry where the heading above it already names the class.
toc_object_entries = True
toc_object_entries_show_parents = "hide"

# The name a class is presented under, by its documented id. `Session` is
# the import every page teaches (`from isocenter import Session`); the
# class is `isocenter.session.DicomSession`, and its ids (and every
# deep link to them) stay that.
_DISPLAY_NAMES = {"isocenter.session.DicomSession": "Session"}

# The mkdocstrings `filters:` the six entity classes carried, kept on
# those six classes only. Applied globally they would also hide
# `TrackedEntity.mark_modified`, `mark_persisted` and `record_phi_status`,
# which the tier-2 section of api/entities.md documents.
_ENTITY_CLASSES = {"Patient", "Study", "Series", "Instance", "DicomItem",
                   "Equipment"}
_ENTITY_FILTERS = (re.compile(r"^_"), re.compile(r"^record_"),
                   re.compile(r"_vouches_for$"), re.compile(r"^mark_"),
                   re.compile(r"^identity_token_is_this_stores$"),
                   re.compile(r"^clear_sequence_items$"))


def _defining_class(obj):
    """The name of the class whose body defines `obj`, or None."""
    for attr in ("fget", "__func__", "func"):
        obj = getattr(obj, attr, obj)
    qualname = getattr(obj, "__qualname__", "")
    module = getattr(obj, "__module__", "")
    if module != "isocenter.entities" or "." not in qualname:
        return None
    return qualname.rsplit(".", 1)[0]


def _skip_entity_member(app, what, name, obj, skip, options):
    if what != "class" or _defining_class(obj) not in _ENTITY_CLASSES:
        return None
    if any(pattern.search(name) for pattern in _ENTITY_FILTERS):
        return True
    return None


class _FactoryDefault:
    """A `field(default_factory=...)` default, spelled as the call."""

    def __init__(self, factory):
        name = getattr(factory, "__name__", "")
        self.text = f"{name}()" if name.isidentifier() else "..."

    def __repr__(self):
        return self.text


def _dataclass_parameters(cls):
    """The parameters a dataclass's page signature shows.

    A `default_factory` default is spelled as its call (`list()`) instead
    of Python's `<factory>`, and a private field (`Instance._pixel_loader`)
    is left out. `<factory>` is not Python, so Sphinx's signature parser
    falls back to splitting the text on commas, and an annotation holding
    a comma inside nested brackets (`List[Tuple[str, str]]`) is cut in two
    on the page with no warning (review of docs/sphinx C2: four classes,
    `autodoc_preserve_defaults` does not help).
    """
    factories = {f.name: f.default_factory for f in dataclasses.fields(cls)
                 if f.default_factory is not dataclasses.MISSING}
    return [param.replace(default=_FactoryDefault(factories[param.name]))
            if param.name in factories else param
            for param in inspect.signature(cls).parameters.values()
            if not param.name.startswith("_")]


def _dataclass_signature(app, what, name, obj, options, signature,
                         return_annotation):
    """`autodoc-process-signature`: every dataclass, through
    `_dataclass_parameters`, formatted as autodoc formats any signature.

    Chosen over `autodoc_typehints = "description"` or `"none"`, which
    change every signature on the site to fix four, and over a
    hand-written signature per directive, which the next dataclass with a
    factory default would not have.
    """
    if what != "class" or signature is None or not dataclasses.is_dataclass(obj):
        return None
    from sphinx.util.inspect import stringify_signature

    shown = inspect.Signature(_dataclass_parameters(obj))
    return (stringify_signature(shown, show_return_annotation=False,
                                unqualified_typehints=True),
            return_annotation)


# --- copy button -----------------------------------------------------------

copybutton_prompt_text = r">>> |\.\.\. "
copybutton_prompt_is_regexp = True

# --- HTML ------------------------------------------------------------------

html_theme = "pydata_sphinx_theme"
html_title = "Isocenter"
html_copy_source = False
html_show_sourcelink = False

# The "clinical ledger" design (#350): docs/_static/ledger.css maps its
# tokens onto pydata's variables, and docs/_templates/layout.html draws the
# home page's hero and pipeline. The three faces come from Google Fonts.
templates_path = ["_templates"]
html_static_path = ["_static"]
html_css_files = [
    "https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,400;"
    "6..72,500&family=Source+Sans+3:wght@400;600&family=JetBrains+Mono:"
    "wght@400;500&display=swap",
    "ledger.css",
]
# The home page is the hero and the ledger sections, full width: no
# section navigation and no "on this page".
html_sidebars = {"index": []}
# Code blocks are dark on the light page, as the design draws them;
# ledger.css sets the palette over this style's.
pygments_light_style = "github-dark"

# The version switcher reads one list for every folder. The deploy step
# sets DOCS_FOLDER to the folder it writes (`1.0`, `dev`); a local build
# is `dev`.
html_theme_options = {
    "github_url": "https://github.com/kvnlng/Isocenter",
    "use_edit_page_button": True,
    # No `theme-switcher`: the site is light only (D7).
    "navbar_end": ["version-switcher", "navbar-icon-links"],
    "switcher": {
        "json_url": "https://kvnlng.github.io/Isocenter/switcher.json",
        "version_match": os.environ.get("DOCS_FOLDER", "dev"),
    },
    "check_switcher": False,
    "show_version_warning_banner": True,
    # Two levels open in "On this page": a page's headings and what sits
    # under them (an API page's members), as the MkDocs site showed.
    "show_toc_level": 2,
    "secondary_sidebar_items": {
        "**": ["page-toc", "edit-this-page", "sourcelink"],
        "index": [],
    },
    "icon_links": [
        {
            "name": "Email support@isocenter.net",
            "url": "mailto:support@isocenter.net",
            "icon": "fa-solid fa-paper-plane",
            "type": "fontawesome",
        },
    ],
}
html_context = {
    "github_user": "kvnlng",
    "github_repo": "Isocenter",
    "github_version": "main",
    "doc_path": "docs",
    "default_mode": "light",
}


def _slug_first(app, doctree):
    """Make each section's MyST slug its first HTML id.

    docutils gives a section its own id first (`frozen-at-1-0`) and the
    anchor HTML writes is the first. The slug MyST computed with
    `myst_heading_slug_func` is the MkDocs id; put it in front.
    """
    from docutils import nodes

    _put_slugs_first(doctree.findall(nodes.section))


def _put_slugs_first(sections):
    for section in sections:
        slug = section.get("slug")
        if not slug:
            continue
        if slug in section["ids"]:
            section["ids"].remove(slug)
        section["ids"].insert(0, slug)


def _present_objects(app, doctree):
    """Present API objects as the MkDocs site did, ids untouched.

    - A class signature has no `class` prefix, and a class named in
      `_DISPLAY_NAMES` shows that name (`Session(persistence_file=None)`).
    - A member's "On this page" entry is its name without `()`, and a
      class's entry is dropped when the section heading over it already
      names it, so its members sit directly under that heading.
    - Each `Variables` field (napoleon's `Attributes:` section) becomes an
      "Attributes:" table of Name, Type and Description.

    Runs before the table-of-contents collector reads the doctree
    (`setup()` connects it at priority 400), since that collector copies
    each signature's `_toc_name` once.
    """
    from docutils import nodes
    from sphinx import addnodes

    for sig in list(doctree.findall(addnodes.desc_signature)):
        if sig.parent.get("objtype") == "class":
            for child in list(sig.children):
                if isinstance(child, addnodes.desc_annotation) \
                        and child.astext().strip() == "class":
                    sig.remove(child)
            shown = _shown_name(sig.get("ids") or [], sig.get("_toc_name", ""))
            if shown != sig.get("_toc_name", ""):
                for name in sig.findall(addnodes.desc_name):
                    for text in list(name.findall(nodes.Text)):
                        text.parent.replace(text, nodes.Text(shown))
                sig["_toc_name"] = shown
            section = next((node for node in _ancestors(sig)
                            if isinstance(node, nodes.section)), None)
            if section is not None and section.children \
                    and isinstance(section[0], nodes.title) \
                    and section[0].astext().strip() == shown:
                sig["_toc_name"] = ""
        else:
            sig["_toc_name"] = _member_toc_name(sig.get("_toc_name", ""))
    for field in list(doctree.findall(nodes.field)):
        if field[0].astext() == "Variables":
            _variables_as_table(field)


def _ancestors(node):
    while node.parent is not None:
        node = node.parent
        yield node


def _shown_name(ids, toc_name):
    """The name a class signature shows: `_DISPLAY_NAMES`, else its own."""
    for anchor in ids:
        if anchor in _DISPLAY_NAMES:
            return _DISPLAY_NAMES[anchor]
    return toc_name


def _member_toc_name(toc_name):
    """A member's "On this page" entry: its name, without `()`."""
    return toc_name[:-2] if toc_name.endswith("()") else toc_name


def _attribute_parts(children):
    """Split napoleon's `name (type) – description` field item.

    `children` is the item's paragraph's children as `(kind, text)` pairs,
    kind being the node's class name. Returns `(name, type_slice,
    description_slice)` as indices into `children`, or None for any other
    shape, which is left as the list it was.
    """
    if not children or children[0][0] != "literal_strong":
        return None
    index, type_slice = 1, slice(1, 1)
    if index < len(children) and children[index] == ("Text", " ("):
        end = next((i for i in range(index + 1, len(children))
                    if children[i] == ("Text", ")")), None)
        if end is None:
            return None
        type_slice, index = slice(index + 1, end), end + 1
    if index >= len(children) or children[index] != ("Text", " – "):
        return None
    return children[0][1], type_slice, slice(index + 1, len(children))


def _variables_as_table(field):
    """Replace a `Variables` field with an "Attributes:" rubric and table."""
    from docutils import nodes

    body = field[1]
    items = (body[0].children if body.children
             and isinstance(body[0], nodes.bullet_list) else [body])
    rows = []
    for item in items:
        paragraph = item[0] if item.children else None
        if not isinstance(paragraph, nodes.paragraph):
            return
        children = list(paragraph.children)
        parts = _attribute_parts([(type(child).__name__, child.astext())
                                  for child in children])
        if parts is None:
            return
        name, type_slice, text_slice = parts
        rows.append((name, [c.deepcopy() for c in children[type_slice]],
                     [c.deepcopy() for c in children[text_slice]]))
    table = nodes.table(classes=["ledger-attributes"])
    group = nodes.tgroup(cols=3)
    table += group
    for width in (2, 2, 6):
        group += nodes.colspec(colwidth=width)
    head = nodes.row()
    for label in ("Name", "Type", "Description"):
        head += nodes.entry("", nodes.paragraph("", label))
    group += nodes.thead("", head)
    rows_node = nodes.tbody()
    for name, kind, text in rows:
        row = nodes.row()
        row += nodes.entry("", nodes.paragraph("", "", nodes.literal(name, name)))
        row += nodes.entry("", nodes.paragraph("", "", *kind))
        row += nodes.entry("", nodes.paragraph("", "", *text))
        rows_node += row
    group += rows_node
    field_list = field.parent
    rubric = nodes.rubric("", "Attributes:")
    field_list.remove(field)
    if field_list.children:
        position = field_list.parent.index(field_list) + 1
        field_list.parent.insert(position, [rubric, table])
    else:
        field_list.replace_self([rubric, table])


# The hooks are pinned by tests/test_the_docs_conf.py, which loads this
# file without Sphinx.
def setup(app):
    app.connect("autodoc-skip-member", _skip_entity_member)
    app.connect("autodoc-process-signature", _dataclass_signature)
    app.connect("doctree-read", _slug_first)
    # Before the table-of-contents collector (priority 500) reads it.
    app.connect("doctree-read", _present_objects, priority=400)

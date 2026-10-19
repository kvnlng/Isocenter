"""Every fragment link in the documentation must land on a real anchor.

The site's build does not cover this before merge. `sphinx-build -W`
fails on a broken cross-page fragment written as a Markdown link, but it
runs only in `docs.yml`, after a tag; and under MkDocs, strict mode
never checked a fragment at all. A `#fragment` that resolves to no
heading on the target page renders as a working-looking link that
scrolls nowhere. That is how the five Quick Reference links in
`docs/configuration.md` pointed at headings which had been renamed and
stayed dead for months (#152) -- they were only found by reading the
page, and nothing in CI would have noticed if they went stale again.

The check renders each page with Python-Markdown and compares the
anchors the links ask for against the ids the renderer emits. Those are
the site's ids too: `docs/_ext/slugs.py` hands MyST Python-Markdown's
own `slugify`, and a `doctree-read` hook makes that slug each section's
first HTML id, so every heading id the MkDocs site published is kept
(docs/sphinx C2). One slug function, two readers -- the #152 lesson:
the render here imports `pm_slug` from that file, so a change to it moves
both the site's ids and the ids links are graded against. A
link to docutils' spelling of a heading (`#frozen-at-1-0` for "Frozen
at 1.0") is red here, because the site's own id is `frozen-at-10`.
`tests/test_the_docs_are_myst.py` refuses the two heading shapes where
the two renderers could still part (emphasis or a link in a heading, a
heading repeated on a page). Two details are load-bearing, both learned
from the #152 fix:

- **Rendered HTML, not raw markdown.** A link whose *text* wraps across
  two source lines (`docs/architecture.md:95` is one) is invisible to a
  line-based regex. Rendering first removes the problem structurally
  instead of asking the regex to grow.

- **The slug comes from the library, never from a local reimplementation.**
  A stdlib-only slugifier was written during the #152 review and its
  emphasis-stripper ate underscores: `add_rule` slugged to `addrule`.
  Neither name was a link target, so it reached the right verdict for the
  wrong reason -- exactly the failure mode CLAUDE.md describes for the
  retired `text_index`. A second answer that can disagree with the real
  one is worse than no answer, so `markdown` is a declared test
  dependency (`setup.py`, `tests` extra) and the real `toc` extension
  assigns the ids.

Two deferrals, listed because a reader who meets one should know it is
a known limit rather than a defect. The first would surface as a
*spurious failure* -- a link this check calls broken that the site
renders fine. The second is the opposite and so the more dangerous kind:
a gap in coverage:

- `docs/api/*.md` hold autodoc directives inside ```` ```{eval-rst} ````
  fences, whose objects' ids (`isocenter.session.DicomSession.export`)
  are generated from the package at build time, so none of them exist
  here. Nothing links into one today.
- A fragment link whose *file* half points outside the checked set is
  skipped by the fragment check rather than resolved. Since #687 the
  file half is `broken_file_links`' business, for a root page
  (`README.md`, `CHANGELOG.md`, `RELEASING.md`) as for a page under
  `docs/`: a missing target is red there. What stays uncovered is the
  fragment of a link into a file that is not a checked page (a `.py`
  file, a page under `docs/superpowers/`), and whether a root page's
  link works where PyPI renders the README.

**The file half of a link (#687).** The site's strict build fails on a
broken relative file link, but it runs only in `docs.yml`, on a tag or a
dispatch, so a broken link fails the release's docs deploy, after the
tag was cut. Measured with mkdocs 1.6.1 over a 17-link probe page,
MkDocs' strict mode also let five kinds through that are dead on the
published site: an absolute `/page.md`, a link to a page the build
excludes, a directory-style `dir/`, and raw-HTML `<a href>` and
`<img src>`. `broken_file_links` covers all of them before merge, with
the rendering `broken_fragment_links` already pays for, and reads the
root pages the site never builds. Not covered, by either check: the
cross-references autodoc generates in `docs/api/*.md` (the deferral
above), toctree entries (`tests/test_the_docs_are_myst.py` reads
those), and external URLs, which are never fetched: a test that needs
the network is not a gate.
"""
import ast
import functools
import html.parser
import importlib.util
import os
import pathlib
import urllib.parse

import markdown
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Design specs and plans are excluded from the site by `exclude_patterns`
# in docs/conf.py, and are dated records rather than maintained pages.
SKIP_DIRECTORIES = {"superpowers"}

# The Python-Markdown extensions the check renders with. Python-Markdown
# is not the site's renderer (MyST is), so this is the set that gives a
# page's headings and links the same reading MyST gives them: `toc`
# assigns ids with the slug function the site uses, `attr_list` and
# `md_in_html` keep a `{#id}` or a raw-HTML block from hiding a heading
# or a link, and `tables` and `fenced_code` keep a heading-shaped line
# inside a table or a fence from being read as a heading.
RENDER_EXTENSIONS = ("toc", "tables", "fenced_code", "attr_list",
                     "md_in_html")

# MyST extensions `docs/conf.py` may enable without changing which ids a
# heading gets or which links a page holds. `colon_fence` bodies are
# unindented page text, so a heading inside `:::{note}` is a heading to
# Python-Markdown too; the two `attrs_*` extensions add classes and
# options to blocks and spans. Anything else stops the check: a new MyST
# extension is a question about anchors a human answers once.
KNOWN_MYST_EXTENSIONS = {"colon_fence", "attrs_inline", "attrs_block"}

# `toc` settings that leave slug generation alone. `slugify` and
# `separator` replace it outright -- either would make every slug computed
# here wrong, silently, so they stop the test rather than being guessed at.
INERT_TOC_OPTIONS = {"permalink", "permalink_title", "anchorlink",
                     "anchorlink_class", "permalink_class", "title",
                     "title_class", "toc_depth", "baselevel", "marker"}


def _conf_values(root=ROOT):
    """`{name: value}` for the literal assignments in `docs/conf.py`.

    Read from the text, never imported: the `tests` environment has no
    Sphinx, and conf.py imports from it.
    """
    tree = ast.parse((root / "docs" / "conf.py").read_text(encoding="utf-8"))
    values = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    try:
                        values[target.id] = ast.literal_eval(node.value)
                    except ValueError:
                        pass
    return values


def _configured_extensions(root=ROOT):
    """`{name: options}` for the render, after checking conf.py's MyST list.

    Returned as a mapping so a caller can inspect the configuration
    rather than assume it.
    """
    myst = set(_conf_values(root).get("myst_enable_extensions", []))
    unknown = sorted(myst - KNOWN_MYST_EXTENSIONS)
    assert not unknown, (
        f"docs/conf.py enables the MyST extensions {unknown}, which this "
        "check does not know about. Decide whether they change the ids "
        "headings get or the links a page holds, and add them to "
        "KNOWN_MYST_EXTENSIONS or teach the render to read them.")
    return {name: {} for name in RENDER_EXTENSIONS}


def _renderer(configured=None):
    """A Markdown renderer whose ids match the ones the site publishes.

    Anything outside the render set stops the test: a new extension is a
    question about slugs that a human has to answer once, which is
    cheaper than this check quietly grading against the wrong ids.
    """
    configured = _configured_extensions() if configured is None else configured

    unknown = sorted(set(configured) - set(RENDER_EXTENSIONS))
    assert not unknown, (
        f"the render was asked for {unknown}, which this check does not "
        "know about. Decide whether it changes the ids headings get "
        "before adding it to RENDER_EXTENSIONS.")

    toc_options = set(configured.get("toc", {}))
    assert toc_options <= INERT_TOC_OPTIONS, (
        f"the render configures toc with {sorted(toc_options - INERT_TOC_OPTIONS)}, "
        "which can replace the slug function. This check would then be "
        "comparing links against slugs the site never emits; teach it the "
        "new setting rather than deleting this assertion.")

    # The site's own slug function, not a copy of it: `toc` calls
    # `slugify(text, separator)`, and `pm_slug` is the one MyST is given.
    toc = {**configured.get("toc", {}),
           "slugify": lambda text, separator: _site_slug()(text)}
    return markdown.Markdown(extensions=sorted(configured),
                             extension_configs={"toc": toc})


@functools.lru_cache(maxsize=None)
def _site_slug(root=ROOT):
    """`pm_slug` from `docs/_ext/slugs.py`, loaded by path: the function
    `docs/conf.py` names as `myst_heading_slug_func`."""
    path = root / "docs" / "_ext" / "slugs.py"
    spec = importlib.util.spec_from_file_location("_isocenter_docs_slugs", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.pm_slug


class _Page(html.parser.HTMLParser):
    """Collects the anchors a rendered page defines and the ones it asks for."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.anchors = set()
        self.links = []
        self.images = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if attributes.get("id"):
            self.anchors.add(attributes["id"])
        # Hand-written `<a name="...">` targets still work in a browser
        # and appear in this tree's older pages.
        if tag == "a" and attributes.get("name"):
            self.anchors.add(attributes["name"])
        if tag == "a" and attributes.get("href"):
            self.links.append(attributes["href"])
        # `![alt](x.png)` and a raw `<img src>` both arrive here (a
        # self-closing `<img />` through `handle_startendtag`'s default).
        if tag == "img" and attributes.get("src"):
            self.images.append(attributes["src"])


def _pages(root):
    """The markdown files whose anchors are checked.

    Root-level files (README, CHANGELOG, CLAUDE.md) plus the site's
    pages. Deliberately not a repo-wide glob: agent worktrees and
    virtualenvs carry markdown of their own.
    """
    found = [path.resolve() for path in sorted(root.glob("*.md"))]
    for path in sorted((root / "docs").rglob("*.md")):
        if SKIP_DIRECTORIES.isdisjoint(path.relative_to(root / "docs").parts):
            found.append(path.resolve())
    return found


def _parse(path, renderer):
    page = _Page()
    renderer.reset()
    page.feed(renderer.convert(path.read_text(encoding="utf-8")))
    return page


@functools.lru_cache(maxsize=None)
def _rendered(root):
    renderer = _renderer(_configured_extensions(root))
    return {path: _parse(path, renderer) for path in _pages(root)}


def _parsed_pages(root):
    """`{path: _Page}` for every checked page under `root`, rendered once.

    Both checks below read this, so a run renders each page once however
    many of them it calls: rendering is the whole cost (`CHANGELOG.md` is
    most of it), and `pytest --changed` selects this file for every `.md`
    change. Cached per resolved root for the life of the process, so **a
    caller builds its whole site before its first call on a root and never
    edits a root between two calls**; each fixture below has its own
    `tmp_path`, so none shares a key.
    """
    return _rendered(root.resolve())


def broken_fragment_links(root):
    """Every `#fragment` link in the docs that lands on no anchor.

    Takes the root so the same code can be run against an older checkout,
    which is how the #152 breakage was reproduced.
    """
    pages = _parsed_pages(root)

    broken = []
    root = root.resolve()
    for path, page in pages.items():
        for href in page.links:
            target, _, fragment = href.partition("#")
            if not fragment or urllib.parse.urlparse(href).scheme:
                continue
            if not target:
                destination = path  # same-page link
            else:
                destination = (path.parent / urllib.parse.unquote(target)).resolve()
                if destination not in pages:
                    # A link to a file outside the checked set: its
                    # fragment cannot be resolved here (the second bullet
                    # in the module docstring). Whether the file is there
                    # at all is `broken_file_links`' question (#687).
                    continue
            if urllib.parse.unquote(fragment) not in pages[destination].anchors:
                broken.append(f"{path.relative_to(root)} -> {href}")
    return sorted(broken)


def _excluded_directories(root):
    """The directories `exclude_patterns` in docs/conf.py keeps off the site.

    Only the form this tree uses is read: `name/**`, a directory under
    `docs/` and everything in it. Any other pattern stops the check, as
    an unknown extension stops `_renderer`: a pattern guessed at is a
    link wrongly passed.
    """
    names = set()
    for pattern in _conf_values(root).get("exclude_patterns", []):
        name = pattern[:-3]
        assert pattern.endswith("/**") and name and not set(name) & set("/*?[!"), (
            f"docs/conf.py's exclude_patterns holds {pattern!r}, a pattern "
            "this check does not read (it reads `name/**`). Teach "
            "`_excluded_directories` the new form rather than deleting "
            "this assertion.")
        names.add(name)
    return names


def _spelled_as_listed(path, root, listings):
    """True when `path` exists under `root` with every component spelled
    as its directory lists it.

    `Path.exists()` is not enough on this project's machines: macOS's
    filesystem is case-insensitive, so `docs/Waveforms.md` "exists" there
    and is a dead link on the published site and on GitHub.
    """
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return False
    here = root
    for part in parts:
        if here not in listings:
            try:
                listings[here] = set(os.listdir(here))
            except (FileNotFoundError, NotADirectoryError):
                listings[here] = set()
        if part not in listings[here]:
            return False
        here = here / part
    return True


def broken_file_links(root):
    """Every relative link or image in the docs whose target is not a file
    the reader will find: `["docs/a.md -> b.md   [no such file]", ...]`.

    The file half of a link; the `#fragment` half is
    `broken_fragment_links`' business. Checked: every `<a href>` and
    `<img src>` of the rendered page that has no scheme, does not start
    `//`, and has a path. The rules, in the order they are tried:

    1. `[absolute]`: a path starting `/`. Every page lives in a version
       folder, so `/x` points outside it; on GitHub it points at the
       repository root.
    2. `[no such file]`: the target, resolved against the page's
       directory, is not in the repository with every component spelled
       as its directory lists it.
    3. `[leaves docs/: not on the site]`: from a page under `docs/`, a
       target outside `docs/`. The file exists; the site does not hold it.
    4. `[excluded from the site]`: from a page under `docs/`, a target
       under a directory `exclude_patterns` names.
    5. `[a directory]`: the target is a directory, which the build does
       not resolve to a page.

    A root page (`README.md`, `CHANGELOG.md`, `RELEASING.md`) is held to
    1, 2 and 5 against the repository, which is how GitHub renders it.
    """
    pages = _parsed_pages(root)
    root = root.resolve()
    docs = root / "docs"
    excluded = _excluded_directories(root)
    listings = {}

    broken = []
    for path, page in pages.items():
        in_docs = docs in path.parents
        for href in page.links + page.images:
            parsed = urllib.parse.urlparse(href)
            if parsed.scheme or href.startswith("//") or not parsed.path:
                continue
            where = f"{path.relative_to(root)} -> {href}"
            if parsed.path.startswith("/"):
                broken.append(f"{where}   [absolute]")
                continue
            # `normpath`, never `resolve()`: resolving would follow the
            # filesystem's own spelling and hide the case rule 2 exists for.
            target = pathlib.Path(os.path.normpath(
                path.parent / urllib.parse.unquote(parsed.path)))
            if not _spelled_as_listed(target, root, listings):
                broken.append(f"{where}   [no such file]")
            elif in_docs and target != docs and docs not in target.parents:
                broken.append(f"{where}   [leaves docs/: not on the site]")
            elif in_docs and excluded & set(target.relative_to(docs).parts):
                broken.append(f"{where}   [excluded from the site]")
            elif target.is_dir():
                broken.append(f"{where}   [a directory]")
    return sorted(broken)


# The fixtures' docs/conf.py: the two settings this check reads, as the
# site sets them. A minimal file rather than a copy of the real one,
# which imports Sphinx-side modules a fixture has no use for.
_FIXTURE_CONF = (
    'myst_enable_extensions = ["colon_fence", "attrs_inline", "attrs_block"]\n'
    'exclude_patterns = ["superpowers/**", "overrides/**", "_build/**", '
    '"_ext/**"]\n')


def _fixture_site(root, body):
    """A one-page site rooted at `root`, with a minimal docs/conf.py."""
    (root / "docs").mkdir(exist_ok=True)
    (root / "docs" / "conf.py").write_text(_FIXTURE_CONF, encoding="utf-8")
    (root / "index.md").write_text(body, encoding="utf-8")


def test_every_documentation_fragment_link_resolves():
    broken = broken_fragment_links(ROOT)
    assert not broken, (
        "These links point at anchors no heading produces, so they render "
        "as links that go nowhere:\n  " + "\n  ".join(broken))


def test_the_check_would_fail_on_a_broken_anchor(tmp_path):
    """The check has to be able to fail, or it pins nothing.

    #152's five dead links survived because every gate that ran on them
    was green. A one-page fixture with one good and one dead anchor keeps
    that from being true of this test too.
    """
    _fixture_site(
        tmp_path,
        "# Title\n\n[good](#a-real-heading)\n[dead](#renamed-away)\n\n"
        "## A Real Heading\n")

    assert broken_fragment_links(tmp_path) == ["index.md -> #renamed-away"]


def test_the_check_sees_a_link_whose_text_wraps_across_lines(tmp_path):
    """`docs/architecture.md:95` is such a link; a line-based regex misses it."""
    _fixture_site(
        tmp_path,
        "# Title\n\nsee [`false` cannot retain\nprivate tags](#no-such-thing).\n")

    assert broken_fragment_links(tmp_path) == ["index.md -> #no-such-thing"]


def test_a_cross_page_fragment_is_resolved_against_the_page_it_names(tmp_path):
    """`docs/architecture.md` links into `configuration.md#private-tags`.

    MkDocs' strict mode checked that `configuration.md` exists and
    stopped there, and the Sphinx build runs only after a tag, so the
    fragment half of a cross-page link needs this check too.
    """
    _fixture_site(tmp_path, "# Title\n")
    (tmp_path / "docs" / "architecture.md").write_text(
        "# Architecture\n\n[real](configuration.md#private-tags)\n"
        "[gone](configuration.md#renamed-away)\n", encoding="utf-8")
    (tmp_path / "docs" / "configuration.md").write_text(
        "# Configuration\n\n## Private Tags\n", encoding="utf-8")

    assert broken_fragment_links(tmp_path) == [
        "docs/architecture.md -> configuration.md#renamed-away"]


def test_the_myst_extension_list_is_read_not_assumed(tmp_path):
    """conf.py's `myst_enable_extensions` is inspected, not assumed.

    Pinned because the temptation is to hardcode today's answer: a MyST
    extension that changes headings or links (`deflist`, `html_admonition`,
    `substitution`) would make the render here grade against the wrong
    page, quietly. An unknown one stops the check.
    """
    configured = _configured_extensions()

    assert "attr_list" in configured
    assert "toc" in configured, "toc is what assigns heading ids"
    # A slugify override has to stop the check rather than be ignored.
    with pytest.raises(AssertionError, match="slug"):
        _renderer({**configured, "toc": {"slugify": "custom"}})

    with pytest.raises(AssertionError, match="does not know"):
        _renderer({**configured, "pymdownx.slugs": {}})

    _fixture_site(tmp_path, "# Root\n")
    assert set(_configured_extensions(tmp_path)) == set(RENDER_EXTENSIONS)
    conf = tmp_path / "docs" / "conf.py"
    conf.write_text(_FIXTURE_CONF.replace('"colon_fence"', '"colon_fence", "deflist"'),
                    encoding="utf-8")
    with pytest.raises(AssertionError, match="deflist"):
        _configured_extensions(tmp_path)


def test_a_link_to_docutils_spelling_of_a_heading_is_red(tmp_path):
    """K1: the site's id for "Frozen at 1.0" is Python-Markdown's.

    docutils would spell it `frozen-at-1-0`; the slug hook keeps
    `frozen-at-10`, the id the MkDocs site published and every deep link
    names. A link to the docutils spelling is a link to an id the page
    does not carry first, and is red here.
    """
    _fixture_site(
        tmp_path,
        "# Title\n\n[kept](#frozen-at-10) [docutils](#frozen-at-1-0)\n"
        "[env](#isocenter_max_tasks_per_child)\n\n## Frozen at 1.0\n\n"
        "## ISOCENTER_MAX_TASKS_PER_CHILD\n")

    assert broken_fragment_links(tmp_path) == ["index.md -> #frozen-at-1-0"]


# -- The file half of a link (#687) ----------------------------------------

def _site(root, files):
    """A fixture site: `files` maps a relative path to its text (or to
    None for an empty directory), under a minimal docs/conf.py. Built
    whole before the first check reads it; see `_parsed_pages`."""
    _fixture_site(root, "# Root\n")
    for name, text in files.items():
        path = root / name
        if text is None:
            path.mkdir(parents=True, exist_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def test_every_documentation_file_link_resolves():
    broken = broken_file_links(ROOT)
    assert not broken, (
        "These links or images point at no file the reader will find (the "
        "bracket says why):\n  " + "\n  ".join(broken))


def test_the_real_tree_holds_links_for_the_check_to_read():
    """An empty reading is a broken check, not a clean one (#299)."""
    pages = _parsed_pages(ROOT)
    relative = [href for page in pages.values() for href in page.links
                if not urllib.parse.urlparse(href).scheme
                and urllib.parse.urlparse(href).path]
    assert len(relative) > 100, len(relative)
    assert any(path.name == "README.md" for path in pages)
    assert any(path.name == "CHANGELOG.md" for path in pages)
    assert any(path.name == "RELEASING.md" for path in pages)


def test_the_check_would_fail_on_a_missing_page(tmp_path):
    _site(tmp_path, {
        "docs/a.md": "# A\n\n[ok](b.md) [gone](gone.md) [frag](gone.md#x)\n"
                     "[deep](tutorials/gone.md) [up](tutorials/../b.md)\n",
        "docs/b.md": "# B\n",
        "docs/tutorials/t.md": "# T\n\n[up](../b.md) [gone](../gone.md)\n",
    })

    assert broken_file_links(tmp_path) == [
        "docs/a.md -> gone.md   [no such file]",
        "docs/a.md -> gone.md#x   [no such file]",
        "docs/a.md -> tutorials/gone.md   [no such file]",
        "docs/tutorials/t.md -> ../gone.md   [no such file]",
    ]


@pytest.mark.parametrize("link, message", [
    ("[x](../RELEASING.md)",
     "docs/a.md -> ../RELEASING.md   [leaves docs/: not on the site]"),
    ("[x](superpowers/specs/x.md)",
     "docs/a.md -> superpowers/specs/x.md   [excluded from the site]"),
    ("[x](/b.md)", "docs/a.md -> /b.md   [absolute]"),
    ("[x](tutorials/)", "docs/a.md -> tutorials/   [a directory]"),
    ("[x](tutorials)", "docs/a.md -> tutorials   [a directory]"),
    ("![i](images/gone.png)",
     "docs/a.md -> images/gone.png   [no such file]"),
    ('<img src="images/gone.png">',
     "docs/a.md -> images/gone.png   [no such file]"),
    ('<img src="images/gone.png" />',
     "docs/a.md -> images/gone.png   [no such file]"),
    ('<a href="gone.md">x</a>', "docs/a.md -> gone.md   [no such file]"),
    ("[r][1]\n\n[1]: gone.md", "docs/a.md -> gone.md   [no such file]"),
    ("[x](../../outside.md)",
     "docs/a.md -> ../../outside.md   [no such file]"),
    ("[x](gone.md?query=1)",
     "docs/a.md -> gone.md?query=1   [no such file]"),
], ids=["leaves-docs", "excluded", "absolute", "directory-slash", "directory",
        "image", "html-img", "html-img-self-closing", "html-a", "reference",
        "outside-the-repository", "query"])
def test_each_kind_of_dead_link_is_named_with_its_reason(tmp_path, link, message):
    """One link per fixture, the message asserted whole. Five of these
    passed MkDocs' strict build (1.6.1): absolute, excluded, a directory,
    and both raw-HTML forms."""
    _site(tmp_path, {
        "docs/a.md": f"# A\n\n{link}\n",
        "docs/b.md": "# B\n",
        "docs/tutorials/t.md": "# T\n",
        "docs/images/here.png": "",
        "docs/superpowers/specs/x.md": "# Spec\n",
        "RELEASING.md": "# Releasing\n",
    })

    assert broken_file_links(tmp_path) == [message]


def test_a_link_spelled_in_the_wrong_case_is_broken(tmp_path):
    """`B.md` for `b.md`. On a case-sensitive filesystem `exists()` says
    no by itself; on macOS it says yes, and only the comparison with the
    directory's own listing flags the link. Skipped nowhere."""
    _site(tmp_path, {
        "docs/a.md": "# A\n\n[x](B.md) [y](Tutorials/t.md) [ok](b.md)\n",
        "docs/b.md": "# B\n",
        "docs/tutorials/t.md": "# T\n",
    })

    assert broken_file_links(tmp_path) == [
        "docs/a.md -> B.md   [no such file]",
        "docs/a.md -> Tutorials/t.md   [no such file]",
    ]


def test_links_the_check_leaves_alone(tmp_path):
    _site(tmp_path, {
        "docs/a.md": (
            "# A\n\n[web](https://example.org/gone.md) "
            "[mail](mailto:someone@example.org) [host](//example.org/x.md)\n"
            "[same](#a) [frag](b.md#b) [wrapped\ntext](b.md)\n"
            "[encoded](b%2Emd) ![img](images/here.png)\n"
            '<a href="b.md">raw</a> <img src="images/here.png">\n'
            "[code](tutorials/t.md)\n\n```\n[not a link](gone.md)\n```\n"),
        "docs/b.md": "# B\n",
        "docs/tutorials/t.md": "# T\n\n[up](../b.md) [img](../images/here.png)\n",
        "docs/images/here.png": "",
        "README.md": "# Readme\n\n[docs](docs/a.md) [rel](RELEASING.md) "
                     "[spec](docs/superpowers/specs/x.md) [src](src/x.py)\n",
        "RELEASING.md": "# Releasing\n\n[back](README.md#readme)\n",
        "docs/superpowers/specs/x.md": "# Spec\n\n[dead](gone.md)\n",
        "src/x.py": "",
    })

    assert broken_file_links(tmp_path) == []


def test_a_root_page_link_to_a_missing_file_is_broken(tmp_path):
    """The site never builds README.md, CHANGELOG.md or RELEASING.md."""
    _site(tmp_path, {
        "docs/a.md": "# A\n",
        "README.md": "# Readme\n\n[ok](docs/a.md) [gone](docs/gone.md) "
                     "[abs](/docs/a.md) [dir](docs/) [case](docs/A.md)\n",
    })

    assert broken_file_links(tmp_path) == [
        "README.md -> /docs/a.md   [absolute]",
        "README.md -> docs/   [a directory]",
        "README.md -> docs/A.md   [no such file]",
        "README.md -> docs/gone.md   [no such file]",
    ]


def test_an_exclude_pattern_the_check_cannot_read_stops_it(tmp_path):
    _site(tmp_path, {"docs/a.md": "# A\n"})
    assert _excluded_directories(tmp_path) == {
        "superpowers", "overrides", "_build", "_ext"}
    assert _excluded_directories(ROOT) == _excluded_directories(tmp_path), (
        "the fixtures' conf.py and the site's disagree on exclude_patterns")

    conf = tmp_path / "docs" / "conf.py"
    conf.write_text(_FIXTURE_CONF.replace('"_ext/**"', '"_ext/**", "drafts/*.md"'),
                    encoding="utf-8")
    with pytest.raises(AssertionError, match="does not read"):
        _excluded_directories(tmp_path)


def test_the_two_checks_render_each_page_once(tmp_path, monkeypatch):
    """The link check costs no second rendering: `pytest --changed`
    selects this file for every `.md` change, and rendering is the cost."""
    _site(tmp_path, {
        "docs/a.md": "# A\n\n[b](b.md#b)\n",
        "docs/b.md": "# B\n",
        "README.md": "# Readme\n",
    })
    converted = []
    convert = markdown.Markdown.convert

    def counting(self, source):
        converted.append(source)
        return convert(self, source)

    monkeypatch.setattr(markdown.Markdown, "convert", counting)

    assert broken_fragment_links(tmp_path) == []
    assert broken_file_links(tmp_path) == []
    assert broken_fragment_links(tmp_path) == []
    # index.md and README.md at the root, a.md and b.md under docs/.
    assert len(converted) == 4

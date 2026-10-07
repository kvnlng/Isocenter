"""Every fragment link in the documentation must land on a real anchor.

`mkdocs build --strict` does not cover this. Strict mode fails on a broken
*file* reference; a `#fragment` that resolves to no heading on the target
page renders as a working-looking link that scrolls nowhere. That is how
the five Quick Reference links in `docs/configuration.md` pointed at
headings which had been renamed and stayed dead for months (#152) -- they
were only found by reading the page, and nothing in CI would have
noticed if they went stale again.

The check renders each page with Python-Markdown, configured from
`mkdocs.yml`, and compares the anchors the links ask for against the ids
the renderer actually emits. Two details are load-bearing, both learned
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

Four deferrals, listed because a reader who meets one should know it is
a known limit rather than a defect. The first three would surface as a
*spurious failure* -- a link this check calls broken that the site
renders fine -- and are places where the renderer here is
Python-Markdown alone rather than the whole mkdocs pipeline. The fourth
is the opposite and so the more dangerous kind: a gap in coverage:

- `pymdownx.snippets` is enabled, and an anchor inside a `--8<--`
  included fragment resolves against the *including* page. There are
  zero includes in the tree today; when the first one lands, expand the
  extension into the page body before rendering rather than
  special-casing the link.
- `docs/api/*.md` are mkdocstrings stubs (`::: isocenter.session`) whose
  headings are generated from docstrings at build time, so none of their
  ids exist here. Nothing links into one today.
- `pymdownx.tabbed` is not loaded (see below), so a heading inside a
  `=== "Tab"` body is indented content here and emits no id. `admonition`
  had the same property and is core Python-Markdown, so it is loaded.
- A fragment link whose *file* half points outside the checked set is
  skipped by the fragment check rather than resolved. Since #687 the
  file half is `broken_file_links`' business, for a root page
  (`README.md`, `CHANGELOG.md`, `RELEASING.md`) as for a page under
  `docs/`: a missing target is red there. What stays uncovered is the
  fragment of a link into a file that is not a checked page (a `.py`
  file, a page under `docs/superpowers/`), and whether a root page's
  link works where PyPI renders the README.

**The file half of a link (#687).** `mkdocs build --strict` does fail on
a broken relative file link, but it runs only in `docs.yml`, on a tag or
a dispatch, so a broken link used to fail the release's docs deploy,
after the tag was cut. Measured with mkdocs 1.6.1 over a 17-link probe
page, strict mode also lets five kinds through that are dead on the
published site: an absolute `/page.md`, a link to a page `exclude_docs`
keeps off the site, a directory-style `dir/`, and raw-HTML `<a href>` and
`<img src>`. `broken_file_links` covers all of them before merge, with
the rendering `broken_fragment_links` already pays for, and reads the
root pages strict mode never builds. Not covered, by either check: a
link inside a `pymdownx.snippets` include or a tab body (the deferrals
above), the cross-references mkdocstrings generates in `docs/api/*.md`,
the `nav` entries of `mkdocs.yml`, and external URLs, which are never
fetched: a test that needs the network is not a gate.
"""
import functools
import html.parser
import os
import pathlib
import urllib.parse

import markdown
import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Design specs and plans are excluded from the site by `exclude_docs` in
# mkdocs.yml, and are dated records rather than maintained pages.
SKIP_DIRECTORIES = {"superpowers"}

# mkdocs enables these three itself, on top of whatever mkdocs.yml lists.
MKDOCS_BUILTIN_EXTENSIONS = ["toc", "tables", "fenced_code"]

# Extensions from mkdocs.yml that are not loaded here. All are
# third-party -- `pymdown-extensions` ships with the `docs` extra, not
# `tests` -- so loading them would make this test's result depend on
# which extra happens to be installed. Four only style code blocks and
# cannot change which ids a page emits. `pymdownx.tabbed` can: without
# it a heading inside a `=== "Tab"` body reads as indented content and
# emits no id, so a link to one would be reported broken here. Nothing
# links into a tab body today (see the deferrals above).
INERT_EXTENSIONS = {
    "pymdownx.highlight",
    "pymdownx.inlinehilite",
    "pymdownx.snippets",
    "pymdownx.superfences",
    "pymdownx.tabbed",
}

# Extensions that can affect anchors and are core Python-Markdown, so
# they are loaded for real. `attr_list` is the one that matters most: it
# is what makes `## Heading {#custom-id}` name its own anchor.
# `admonition` is here rather than in the inert set because without it an
# admonition body parses as an indented code block, so a heading inside
# one would silently emit no id.
ANCHOR_RELEVANT_EXTENSIONS = {"attr_list", "md_in_html", "toc", "tables",
                              "fenced_code", "footnotes", "abbr", "def_list",
                              "admonition"}

# `toc` settings that leave slug generation alone. `slugify` and
# `separator` replace it outright, and `pymdownx.slugs` exists only to
# supply a different one -- either would make every slug computed here
# wrong, silently, so they stop the test rather than being guessed at.
INERT_TOC_OPTIONS = {"permalink", "permalink_title", "anchorlink",
                     "anchorlink_class", "permalink_class", "title",
                     "title_class", "toc_depth", "baselevel", "marker"}


class _MkdocsYaml(yaml.SafeLoader):
    """SafeLoader that tolerates mkdocs.yml's python-object tags.

    `pymdownx.superfences`' mermaid fence is configured with
    `!!python/name:...`, which SafeLoader rejects outright. The value is
    irrelevant here; only the extension names are read.
    """


_MkdocsYaml.add_multi_constructor(
    None, lambda loader, suffix, node: None)


def _configured_extensions(root=ROOT):
    """Extension names and their options, as mkdocs would apply them.

    Returned as `{name: options}` so the caller can inspect the config
    rather than assume it. mkdocs.yml lists extensions as a YAML sequence
    whose entries are either a bare string or a single-key mapping of
    name to options.
    """
    config = yaml.load((root / "mkdocs.yml").read_text(encoding="utf-8"),
                       Loader=_MkdocsYaml)
    extensions = {name: {} for name in MKDOCS_BUILTIN_EXTENSIONS}
    for entry in config.get("markdown_extensions") or []:
        if isinstance(entry, str):
            extensions.setdefault(entry, {})
        else:
            for name, options in entry.items():
                extensions[name] = options or {}
    return extensions


def _renderer(configured=None):
    """A Markdown renderer whose ids match the ones mkdocs will publish.

    Anything not known to be anchor-irrelevant stops the test: a new
    extension is a question about slugs that a human has to answer once,
    which is cheaper than this check quietly grading against the wrong
    ids.
    """
    configured = _configured_extensions() if configured is None else configured

    unknown = sorted(set(configured) - INERT_EXTENSIONS
                     - ANCHOR_RELEVANT_EXTENSIONS)
    assert not unknown, (
        f"mkdocs.yml enables {unknown}, which this check does not know "
        "about. Decide whether it changes the ids headings get: add it to "
        "INERT_EXTENSIONS if not, or to ANCHOR_RELEVANT_EXTENSIONS (and "
        "make sure it is installed by the `tests` extra) if it does.")

    toc_options = set(configured.get("toc", {}))
    assert toc_options <= INERT_TOC_OPTIONS, (
        f"mkdocs.yml configures toc with {sorted(toc_options - INERT_TOC_OPTIONS)}, "
        "which can replace the slug function. This check would then be "
        "comparing links against slugs the site never emits; teach it the "
        "new setting rather than deleting this assertion.")

    return markdown.Markdown(
        extensions=sorted(set(configured) & ANCHOR_RELEVANT_EXTENSIONS),
        extension_configs={"toc": configured.get("toc", {})})


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
                    # fragment cannot be resolved here (the fourth bullet
                    # in the module docstring). Whether the file is there
                    # at all is `broken_file_links`' question (#687).
                    continue
            if urllib.parse.unquote(fragment) not in pages[destination].anchors:
                broken.append(f"{path.relative_to(root)} -> {href}")
    return sorted(broken)


def _excluded_directories(root):
    """The directories `exclude_docs` in mkdocs.yml keeps off the site.

    `exclude_docs` is a block of gitignore-style patterns. Only the form
    this tree uses is read: a bare directory name with a trailing slash
    (`superpowers/`), which excludes that directory at any depth. Any
    other pattern stops the check, as an unknown extension stops
    `_renderer`: a pattern guessed at is a link wrongly passed.
    """
    config = yaml.load((root / "mkdocs.yml").read_text(encoding="utf-8"),
                       Loader=_MkdocsYaml)
    names = set()
    for line in (config.get("exclude_docs") or "").splitlines():
        pattern = line.strip()
        if not pattern or pattern.startswith("#"):
            continue
        name = pattern[:-1]
        assert pattern.endswith("/") and name and not set(name) & set("/*?[!"), (
            f"mkdocs.yml's exclude_docs holds {pattern!r}, a pattern this "
            "check does not read (it reads `name/`, a directory at any "
            "depth). Teach `_excluded_directories` the new form rather "
            "than deleting this assertion.")
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

    1. `[absolute]`: a path starting `/`. Under mike every page lives in
       a version folder, so `/x` points outside it; on GitHub it points at
       the site root.
    2. `[no such file]`: the target, resolved against the page's
       directory, is not in the repository with every component spelled
       as its directory lists it.
    3. `[leaves docs/: not on the site]`: from a page under `docs/`, a
       target outside `docs/`. The file exists; the site does not hold it.
    4. `[excluded from the site]`: from a page under `docs/`, a target
       under a directory `exclude_docs` names.
    5. `[a directory]`: the target is a directory, which mkdocs does not
       resolve to a page.

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


def _fixture_site(root, body):
    """A one-page site rooted at `root`, carrying this repo's mkdocs.yml.

    The real config is copied rather than a minimal one written, so a
    fixture cannot pass under extension settings the site does not use.
    """
    (root / "docs").mkdir(exist_ok=True)
    (root / "mkdocs.yml").write_text(
        (ROOT / "mkdocs.yml").read_text(encoding="utf-8"), encoding="utf-8")
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

    mkdocs --strict checks that `configuration.md` exists and stops
    there, so the fragment half of a cross-page link is unguarded too.
    """
    _fixture_site(tmp_path, "# Title\n")
    (tmp_path / "docs" / "architecture.md").write_text(
        "# Architecture\n\n[real](configuration.md#private-tags)\n"
        "[gone](configuration.md#renamed-away)\n", encoding="utf-8")
    (tmp_path / "docs" / "configuration.md").write_text(
        "# Configuration\n\n## Private Tags\n", encoding="utf-8")

    assert broken_fragment_links(tmp_path) == [
        "docs/architecture.md -> configuration.md#renamed-away"]


def test_mkdocs_yaml_extension_config_is_read_not_assumed():
    """The slug depends on the configured extensions, so they are inspected.

    Pinned because the temptation is to hardcode today's answer: there is
    no slugify override in mkdocs.yml right now, and a check that assumed
    that would go quietly wrong the day one is added.
    """
    configured = _configured_extensions()

    assert "attr_list" in configured
    assert "toc" in configured, (
        "toc is a mkdocs builtin extension and is what assigns heading ids")
    # A slugify override has to stop the check rather than be ignored.
    with pytest.raises(AssertionError, match="slug"):
        _renderer({**configured, "toc": {"slugify": "custom"}})

    with pytest.raises(AssertionError, match="does not know"):
        _renderer({**configured, "pymdownx.slugs": {}})


# -- The file half of a link (#687) ----------------------------------------

def _site(root, files):
    """A fixture site: `files` maps a relative path to its text (or to
    None for an empty directory), under this repo's mkdocs.yml. Built
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
    pass `mkdocs build --strict` (1.6.1): absolute, excluded, a directory,
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
    """`mkdocs build` never reads README.md, CHANGELOG.md or RELEASING.md."""
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


def test_an_exclude_docs_pattern_the_check_cannot_read_stops_it(tmp_path):
    _site(tmp_path, {"docs/a.md": "# A\n"})
    assert _excluded_directories(tmp_path) == {"superpowers", "overrides"}

    config = (tmp_path / "mkdocs.yml").read_text(encoding="utf-8")
    assert "  superpowers/\n" in config
    (tmp_path / "mkdocs.yml").write_text(
        config.replace("  superpowers/\n", "  superpowers/\n  drafts/*.md\n"),
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

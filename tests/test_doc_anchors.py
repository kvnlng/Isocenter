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

Three deferrals, listed because each would surface as a *spurious
failure* -- a link this check calls broken that the site renders fine --
and a reader who meets one should know it is a known limit rather than a
defect. All three are places where the renderer here is Python-Markdown
alone rather than the whole mkdocs pipeline:

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
"""
import html.parser
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


def broken_fragment_links(root):
    """Every `#fragment` link in the docs that lands on no anchor.

    Takes the root so the same code can be run against an older checkout,
    which is how the #152 breakage was reproduced.
    """
    renderer = _renderer(_configured_extensions(root))
    pages = {path: _parse(path, renderer) for path in _pages(root)}

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
                    # A link to a file outside the checked set; mkdocs
                    # --strict already fails on a missing one.
                    continue
            if urllib.parse.unquote(fragment) not in pages[destination].anchors:
                broken.append(f"{path.relative_to(root)} -> {href}")
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

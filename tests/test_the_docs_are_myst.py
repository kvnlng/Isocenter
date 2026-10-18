"""The documentation is MyST for Sphinx, and no MkDocs syntax survives in it.

The site moved from MkDocs/Material/mkdocstrings to Sphinx with MyST and
pydata-sphinx-theme (docs/sphinx). Most of what MkDocs understood and
MyST does not is not an error to Sphinx: a `!!! warning` line renders as
a paragraph holding three exclamation marks, a `=== "macOS"` tab as a
heading underline, a `::: isocenter.x` stub as text, and a ```` ```{autoclass} ````
fence as a page showing `.. py:method::` lines. `sphinx-build -W` stays
green over all of them. So each is refused here, by file and line,
before merge.

Scope: every `docs/**/*.md` page Sphinx builds, which is every page but
those under a directory `docs/conf.py`'s `exclude_patterns` names.

1. No admonition opener (`!!!`, `???`). Use `:::{note}`, or
   `:::{admonition} Title` with `:class: warning`.
2. No tab opener (`=== "..."`). Use `::::{tab-set}` and `:::{tab-item}`.
3. No mkdocstrings stub (`::: package.module`).
4. No autodoc directive as a MyST fence (```` ```{autoclass} ````).
   Autodoc emits reST, which a MyST fence parses as Markdown: the page
   shows the directives as text with no warning. Autodoc lives inside
   ```` ```{eval-rst} ````.
5. No `--8<--` snippet include.
6. No MkDocs front-matter key (`template`, `hide`).
7. A heading's id is the same on both renderers. MyST's slug reads a
   heading's plain text and code only, and Python-Markdown's reads all
   of it, so no emphasis or link inside a heading. And no heading text
   twice on one page, because the two suffix a repeat differently
   (`-1`, `_1`). `tests/test_doc_anchors.py` checks links against
   Python-Markdown's ids, and `docs/_ext/slugs.py` gives the site the
   same ones; these two shapes are where they could still part.
8. `docs/conf.py` keeps the settings whose loss is silent, read from
   its text, never imported (the `tests` environment has no Sphinx):
   `default_role = "literal"` (without it every single-backtick span in
   a docstring renders as italic `<cite>`); the slug function and the
   `doctree-read` hook that keep every published heading id;
   `html_copy_source = False`; and the light-only theme (D7): no
   `theme-switcher`, `default_mode` light.
9. Every page is reachable from the root toctree, and every toctree
   entry names a page that exists. A page in no toctree is built but
   linked from nowhere, and Sphinx says so only as a warning.

No `scripts/mutation_probe.py` `TARGETS` row: this reads documentation
text and imports no package module. It spells no dotted module name of
the package either (fixtures say `pkg.x`), because
`tests/test_mutation_probe_targets.py` reads one as an import.
"""
import ast
import pathlib
import re

import markdown
import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"
CONF = DOCS / "conf.py"

_FENCE = re.compile(r"^(?P<fence>`{3,}|~{3,})")
_TOCTREE = re.compile(r"^(?P<fence>`{3,}|:{3,})\{toctree\}[^\n]*\n(?P<body>.*?)^(?P=fence)[ \t]*$",
                      re.MULTILINE | re.DOTALL)
_ENTRY = re.compile(r"^(?:(?P<title>.*?)\s*<(?P<target>[^<>]+)>|(?P<bare>\S+))$")

RULES = {
    "admonition": (re.compile(r"^\s*(!!!|\?\?\?\+?)\s*\w"),
                   "a MkDocs admonition; use :::{note} or "
                   ":::{admonition} Title (rule 1)"),
    "tab": (re.compile(r'^\s*===\+?\s+"'),
            "a MkDocs tab; use ::::{tab-set} and :::{tab-item} (rule 2)"),
    "stub": (re.compile(r"^:::\s+[\w.]+\s*$"),
             "a mkdocstrings stub; use an autodoc directive inside "
             "```{eval-rst} (rule 3)"),
    "autodoc-fence": (re.compile(
        r"^\s*(`{3,}|:{3,})\{auto(class|module|function|data|exception"
        r"|method|attribute)\}"),
        "an autodoc directive as a MyST fence, which renders its reST as "
        "text; put it inside ```{eval-rst} (rule 4)"),
    "snippet": (re.compile(r"-+8<-+"),
                "a pymdownx.snippets include, which MyST does not read "
                "(rule 5)"),
}


def _conf_tree(path=CONF):
    return ast.parse(path.read_text(encoding="utf-8"), str(path))


def _conf_values(path=CONF):
    """`{name: value}` for every top-level literal assignment in conf.py."""
    values = {}
    for node in _conf_tree(path).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    try:
                        values[target.id] = ast.literal_eval(node.value)
                    except ValueError:
                        values[target.id] = None
    return values


def _dict_entry(path, name, key):
    """`name[key]` in conf.py, where `name` is a dict display whose other
    entries need not be literals (`html_theme_options` reads the
    environment)."""
    for node in _conf_tree(path).body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict)
                and any(isinstance(t, ast.Name) and t.id == name
                        for t in node.targets)):
            for k, v in zip(node.value.keys, node.value.values):
                if isinstance(k, ast.Constant) and k.value == key:
                    return ast.literal_eval(v)
    return None


def _excluded(path=CONF):
    """The directories `exclude_patterns` names, as `dir/**` patterns."""
    names = set()
    for pattern in _conf_values(path)["exclude_patterns"]:
        assert pattern.endswith("/**") and "/" not in pattern[:-3], (
            f"docs/conf.py's exclude_patterns holds {pattern!r}; this test "
            "reads `name/**` only. Teach it the new form.")
        names.add(pattern[:-3])
    return names


def site_pages(docs=DOCS):
    """Every page Sphinx builds from `docs`, sorted."""
    excluded = _excluded(docs / "conf.py")
    return sorted(path for path in docs.rglob("*.md")
                  if excluded.isdisjoint(path.relative_to(docs).parts))


def _lines_outside_code(text):
    """`(line number, line)` for each line outside a backtick fence.

    A colon fence (`:::{note}`) is MyST's directive syntax and its body
    is page text, so it is read. A backtick fence's body is code, except
    that its opening line is itself graded (rule 4 is about one).
    """
    fence = None
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        match = _FENCE.match(stripped)
        if fence is None:
            if match:
                fence = match.group("fence")
            yield number, line
        elif match and stripped.startswith(fence) and not stripped[len(fence):].strip():
            fence = None


def syntax_offenders(path, docs=DOCS):
    """Rules 1 to 6 over one page, as `rel:line: message` strings."""
    text = path.read_text(encoding="utf-8")
    rel = path.relative_to(docs).as_posix()
    found = []
    for number, line in _lines_outside_code(text):
        for regex, message in RULES.values():
            if regex.search(line):
                found.append(f"{rel}:{number}: {message}")
    if text.startswith("---\n"):
        front = text.split("---\n", 2)[1]
        for key in ("template", "hide"):
            if re.search(rf"^{key}\s*:", front, re.MULTILINE):
                found.append(f"{rel}:1: front matter sets `{key}`, a "
                             "MkDocs key MyST ignores (rule 6)")
    return found


def heading_offenders(path, docs=DOCS):
    """Rule 7 over one page, read through Python-Markdown's render."""
    rel = path.relative_to(docs).as_posix()
    renderer = markdown.Markdown(extensions=["toc", "tables", "fenced_code",
                                             "attr_list", "md_in_html"])
    html = renderer.convert(path.read_text(encoding="utf-8"))
    found = []
    seen = {}
    for match in re.finditer(r'<h([1-6]) id="([^"]+)">(.*?)</h\1>', html,
                             re.DOTALL):
        heading_id, inner = match.group(2), match.group(3)
        if re.search(r"<(em|strong|a)\b", inner):
            found.append(f"{rel}: heading {heading_id!r} holds emphasis or "
                         "a link, which MyST's slug leaves out and "
                         "Python-Markdown's keeps (rule 7)")
        text = re.sub(r"<[^>]+>", "", inner).strip()
        if text in seen:
            found.append(f"{rel}: the heading {text!r} appears twice; MyST "
                         "and Python-Markdown suffix a repeat differently "
                         "(rule 7)")
        seen[text] = heading_id
    return found


def toctree_entries(path):
    """`[(target page path or URL, line)]` for each toctree entry on a page."""
    text = path.read_text(encoding="utf-8")
    entries = []
    for block in _TOCTREE.finditer(text):
        for line in block.group("body").splitlines():
            line = line.strip()
            if not line or line.startswith(":"):
                continue
            match = _ENTRY.match(line)
            target = match.group("target") or match.group("bare")
            if re.match(r"[a-z]+://", target):
                entries.append((target, line))
                continue
            entries.append(((path.parent / f"{target}.md").resolve(), line))
    return entries


def reachability_offenders(docs=DOCS):
    """Rule 9: pages no toctree reaches from `index.md`, and dead entries."""
    pages = {path.resolve() for path in site_pages(docs)}
    root = (docs / "index.md").resolve()
    found = []
    reached = {root}
    queue = [root]
    while queue:
        page = queue.pop()
        for target, line in toctree_entries(page):
            if isinstance(target, str):
                continue
            if target not in pages:
                found.append(f"{page.relative_to(docs.resolve()).as_posix()}: "
                             f"toctree entry {line!r} names no page (rule 9)")
                continue
            if target not in reached:
                reached.add(target)
                queue.append(target)
    for page in sorted(pages - reached):
        found.append(f"{page.relative_to(docs.resolve()).as_posix()}: in no "
                     "toctree reachable from index.md (rule 9)")
    return found


def conf_offenders(path=CONF):
    """Rule 8, read from conf.py's text."""
    values = _conf_values(path)
    found = []
    expect = {"default_role": "literal",
              "myst_heading_slug_func": "slugs.pm_slug",
              "html_copy_source": False}
    for name, value in expect.items():
        if values.get(name, object()) != value:
            found.append(f"conf.py: {name} is not {value!r} (rule 8)")
    connects = [node for node in ast.walk(_conf_tree(path))
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "connect" and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "doctree-read"]
    if not connects:
        found.append("conf.py: nothing connects to `doctree-read`, so the "
                     "slug hook does not run (rule 8)")
    navbar_end = _dict_entry(path, "html_theme_options", "navbar_end")
    if navbar_end is None:
        found.append("conf.py: html_theme_options sets no navbar_end, and "
                     "pydata's default holds theme-switcher; the site is "
                     "light only (D7, rule 8)")
    elif "theme-switcher" in navbar_end:
        found.append("conf.py: navbar_end holds theme-switcher; the site "
                     "is light only (D7, rule 8)")
    if (values.get("html_context") or {}).get("default_mode") != "light":
        found.append("conf.py: html_context's default_mode is not "
                     "'light' (D7, rule 8)")
    return found


# --- The sweeps --------------------------------------------------------------

_PAGES = site_pages()


def test_there_are_pages_to_read():
    """#299: an empty scope is a broken walk."""
    assert len(_PAGES) >= 35, _PAGES
    assert (DOCS / "index.md") in _PAGES


def test_no_page_holds_mkdocs_syntax():
    found = [offender for page in _PAGES for offender in syntax_offenders(page)]
    assert not found, "\n  ".join(["MkDocs syntax on the Sphinx site:"] + found)


def test_every_heading_has_one_id_on_both_renderers():
    found = [offender for page in _PAGES for offender in heading_offenders(page)]
    assert not found, "\n  ".join(found)


def test_conf_keeps_the_settings_whose_loss_is_silent():
    assert conf_offenders() == []


def test_every_page_is_reachable_from_the_root_toctree():
    assert reachability_offenders() == []


# --- The rules on fixtures ---------------------------------------------------

def _site(root, pages, conf=None):
    docs = root / "docs"
    for name, text in pages.items():
        (docs / name).parent.mkdir(parents=True, exist_ok=True)
        (docs / name).write_text(text, encoding="utf-8")
    (docs / "conf.py").write_text(
        conf if conf is not None else CONF.read_text(encoding="utf-8"),
        encoding="utf-8")
    return docs


@pytest.mark.parametrize("line, rule", [
    ('!!! tip "Do this"', "rule 1"),
    ("!!! note", "rule 1"),
    ("    ??? warning", "rule 1"),
    ('=== "macOS"', "rule 2"),
    ('===+ "Linux"', "rule 2"),
    ("::: pkg.exporters", "rule 3"),
    ("```{autoclass} isocenter.x.Y", "rule 4"),
    ("::::{automodule} isocenter.x", "rule 4"),
    ('--8<-- "x.md"', "rule 5"),
])
def test_each_mkdocs_shape_is_red(tmp_path, line, rule):
    docs = _site(tmp_path, {"index.md": f"# Home\n\n{line}\n"})
    found = syntax_offenders(docs / "index.md", docs)
    assert len(found) == 1 and rule in found[0], found
    assert found[0].startswith("index.md:3: "), found


def test_myst_shapes_and_code_are_clean(tmp_path):
    docs = _site(tmp_path, {"index.md": (
        "# Home\n\n:::{note}\nText.\n:::\n\n"
        ":::{admonition} Keep it local\n:class: warning\n\nText.\n:::\n\n"
        "::::{tab-set}\n:::{tab-item} macOS\n:sync: macos\n\nText.\n:::\n::::\n\n"
        "```{eval-rst}\n.. autoclass:: isocenter.x.Y\n   :members: a, b\n```\n\n"
        "```text\n!!! note\n=== \"macOS\"\n::: isocenter.x\n```\n")})
    assert syntax_offenders(docs / "index.md", docs) == []


def test_mkdocs_front_matter_is_red(tmp_path):
    docs = _site(tmp_path, {"index.md": (
        "---\ntitle: Home\ntemplate: home.html\nhide:\n  - toc\n---\n\n# Home\n")})
    found = syntax_offenders(docs / "index.md", docs)
    assert len(found) == 2 and all("rule 6" in f for f in found), found


@pytest.mark.parametrize("heading", [
    "## *Frozen* at 1.0", "## The `x` and _y_", "## See [this](a.md)",
    "## **Strong** words"])
def test_a_heading_with_emphasis_or_a_link_is_red(tmp_path, heading):
    docs = _site(tmp_path, {"index.md": f"# Home\n\n{heading}\n"})
    found = heading_offenders(docs / "index.md", docs)
    assert len(found) == 1 and "rule 7" in found[0], found


def test_a_repeated_heading_is_red_and_code_or_underscores_are_not(tmp_path):
    docs = _site(tmp_path, {"index.md": (
        "# Home\n\n## Verify\n\n## Verify\n\n## `check_burned_in=True`\n\n"
        "## ISOCENTER_MAX_TASKS_PER_CHILD\n")})
    found = heading_offenders(docs / "index.md", docs)
    assert len(found) == 1 and "appears twice" in found[0], found


def test_conf_rules(tmp_path):
    good = CONF.read_text(encoding="utf-8")
    assert conf_offenders(CONF) == []
    cases = {
        'default_role = "literal"': 'default_role = None',
        'myst_heading_slug_func = "slugs.pm_slug"': 'myst_heading_slug_func = None',
        "html_copy_source = False": "html_copy_source = True",
        '"default_mode": "light"': '"default_mode": "auto"',
    }
    for old, new in cases.items():
        assert good.count(old) == 1, old
        path = tmp_path / "conf.py"
        path.write_text(good.replace(old, new), encoding="utf-8")
        assert len(conf_offenders(path)) == 1, (old, conf_offenders(path))
    path.write_text(good.replace('"doctree-read"', '"doctree-resolved"'),
                    encoding="utf-8")
    assert any("doctree-read" in f for f in conf_offenders(path))
    path.write_text(good.replace('"navbar_end": [', '"navbar_end": ["theme-switcher", '),
                    encoding="utf-8")
    assert any("theme-switcher" in f for f in conf_offenders(path))
    path.write_text(good.replace('"navbar_end":', '"navbar_start":'),
                    encoding="utf-8")
    assert any("sets no navbar_end" in f for f in conf_offenders(path))


def test_an_unreachable_page_and_a_dead_entry_are_red(tmp_path):
    docs = _site(tmp_path, {
        "index.md": "# Home\n\n```{toctree}\n:hidden:\n\nguides/index\n"
                    "Changelog <https://example.org/CHANGELOG.md>\n```\n",
        "guides/index.md": "# Guides\n\n```{toctree}\n:maxdepth: 1\n\n"
                           "Configuration <../configuration>\n../gone\n```\n",
        "configuration.md": "# Configuration\n",
        "orphan.md": "# Orphan\n",
        "superpowers/specs/x.md": "# Spec\n",
    })
    assert reachability_offenders(docs) == [
        "guides/index.md: toctree entry '../gone' names no page (rule 9)",
        "orphan.md: in no toctree reachable from index.md (rule 9)",
    ]

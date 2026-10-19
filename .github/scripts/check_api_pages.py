"""Check the built API pages kept what docs/conf.py's `_present_objects` makes.

Usage: python .github/scripts/check_api_pages.py SITE

`_present_objects` rewrites napoleon's output at doctree-read: each
`Variables` field becomes an "Attributes:" table, a class signature loses
its `class` prefix, `DicomSession` shows as `Session`, and a class's own
"On this page" entry goes. A mistake there loses content with a green
`-W` build, and the tests environment has no docutils to run the hook, so
the deploy job runs this against the built site before anything is
pushed. Each problem is printed as an `::error::` line and the exit
status is 1; 0 means none. tests/test_the_api_pages_check.py runs it on
fixture pages.

The row count and each row's type are read from the class's own
docstring (its `Attributes:` section), not from the page, so a table
missing a row or a type does not vouch for itself.
"""
import html
import importlib
import inspect
import pathlib
import re
import sys

# Tables `api/entities/` holds: one per entity class with an Attributes
# section. A new section there changes it; so does losing one.
ENTITY_TABLES = 9

# One row read by name, beside the general check: Session's
# `configuration` is typed `IsocenterConfiguration`.
KNOWN_ROW = ("api/session", "isocenter.session.DicomSession", "configuration",
             "IsocenterConfiguration")

# The class shown under another name, and the member "On this page" must
# list under it.
DISPLAYED = ("api/session", "isocenter.session.DicomSession", "Session",
             "ingest")

_TABLE = re.compile(r'<table class="ledger-attributes[^"]*">(.*?)</table>', re.S)
_OWNER = re.compile(r'<dl class="py class">\s*<dt class="sig sig-object py" id="([^"]+)">')
_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_CELL = re.compile(r"<td>(.*?)</td>", re.S)
_ITEM = re.compile(r"^(\*{0,2}\w+)\s*(?:\((.*?)\))?\s*:(?:\s|$)")
_CLASS_PREFIX = '<span class="property"><span class="k"><span class="pre">class</span>'


def _text(fragment):
    return html.unescape(re.sub(r"<[^>]+>", "", fragment))


def _squeezed(text):
    return re.sub(r"\s+", "", text)


def attribute_items(docstring):
    """`(name, type)` per item of a Google-style `Attributes:` section."""
    lines = inspect.cleandoc(docstring or "").splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == "Attributes:"
                     and not line.startswith(" "))
    except StopIteration:
        return []
    body = []
    for line in lines[start + 1:]:
        if line.strip() and not line.startswith(" "):
            break
        body.append(line)
    indents = [len(line) - len(line.lstrip()) for line in body if line.strip()]
    if not indents:
        return []
    items = []
    for line in body:
        if line.strip() and len(line) - len(line.lstrip()) == min(indents):
            found = _ITEM.match(line.strip())
            items.append((found.group(1), (found.group(2) or "").strip())
                         if found else (line.strip(), ""))
    return items


# The checkout this script sits in: docs/conf.py builds from it (it puts
# the same directory first on sys.path), so the docstrings read here are
# the ones the pages were built from, whatever else is installed.
REPO = pathlib.Path(__file__).resolve().parents[2]


def import_docstring(dotted):
    """The docstring of the object a Python id names, or None."""
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    parts = dotted.split(".")
    for split in range(len(parts) - 1, 0, -1):
        try:
            obj = importlib.import_module(".".join(parts[:split]))
        except ImportError:
            continue
        try:
            for name in parts[split:]:
                obj = getattr(obj, name)
        except AttributeError:
            return None
        return inspect.getdoc(obj)
    return None


def _tables(page):
    """`(owner id, rows, html after the table)` per attributes table."""
    found = []
    for match in _TABLE.finditer(page):
        owners = _OWNER.findall(page, 0, match.start())
        body = match.group(1)
        body = body[body.find("<tbody>"):] if "<tbody>" in body else ""
        rows = [[_text(cell) for cell in _CELL.findall(row)]
                for row in _ROW.findall(body)]
        found.append((owners[-1] if owners else None, rows, page[match.end():]))
    return found


def problems(site, docstring_of=import_docstring, entity_tables=ENTITY_TABLES,
             known_row=KNOWN_ROW, displayed=DISPLAYED):
    site = pathlib.Path(site)
    found = []
    for path in sorted(site.rglob("*.html")):
        if ">Variables<" in path.read_text(encoding="utf-8"):
            found.append(f"{path.relative_to(site)} still has a Variables field list")
    api = sorted((site / "api").glob("*/index.html"))
    if not api:
        return found + [f"{site}/api/ has no pages"]
    tables = {}
    for path in api:
        page = path.read_text(encoding="utf-8")
        rel = path.parent.relative_to(site).as_posix()
        tables[rel] = _tables(page)
        if _CLASS_PREFIX in page:
            found.append(f"{rel}/ has a class signature with its `class` prefix")
        for owner, rows, after in tables[rel]:
            where = f"{rel}/ table of {owner}"
            if owner is None:
                found.append(f"{rel}/ has an attributes table outside any class")
                continue
            if after.lstrip().startswith("</div>"):
                after = after.lstrip()[len("</div>"):]
            if after.lstrip().startswith('<dl class="field-list'):
                found.append(f"{where} comes before the class's other fields")
            docstring = docstring_of(owner)
            items = attribute_items(docstring)
            if not items:
                found.append(f"{where}: no Attributes section is read from "
                             f"{owner}'s docstring" + ("" if docstring else
                                                       " (it was not found)"))
                continue
            if len(rows) != len(items):
                found.append(f"{where} has {len(rows)} rows; its Attributes "
                             f"section has {len(items)} items")
                continue
            for row, (name, kind) in zip(rows, items):
                if len(row) != 3 or row[0].strip() != name.lstrip("*"):
                    found.append(f"{where}: row {row[:1]} is not {name!r}")
                elif _squeezed(row[1]) != _squeezed(kind):
                    found.append(f"{where}: {name}'s type reads {row[1].strip()!r}, "
                                 f"not {kind!r}")
    entities = tables.get("api/entities", [])
    if len(entities) != entity_tables:
        found.append(f"api/entities/ has {len(entities)} attributes tables, "
                     f"not {entity_tables}")
    page_name, owner, name, kind = known_row
    rows = [row for table_owner, table_rows, _ in tables.get(page_name, [])
            if table_owner == owner for row in table_rows]
    if not any(row[:2] and row[0].strip() == name and _squeezed(row[1]) == kind
               for row in rows):
        found.append(f"{page_name}/ has no {name} row typed {kind} under {owner}")
    page_name, owner, shown, member = displayed
    path = site / page_name / "index.html"
    page = path.read_text(encoding="utf-8") if path.is_file() else ""
    signature = re.search(rf'<dt class="sig sig-object py" id="{re.escape(owner)}">\s*'
                          r'(?:<span class="property">.*?</span></span>)?'
                          r'<span class="sig-name descname"><span class="pre">([^<]*)<', page, re.S)
    if not signature or signature.group(1) != shown:
        found.append(f"{page_name}/: {owner} is not shown as {shown}")
    toc = re.search(r'id="pst-page-toc-nav".*?</nav>', page, re.S)
    toc = toc.group(0) if toc else ""
    if f'href="#{owner}"' in toc:
        found.append(f"{page_name}/: On this page lists {owner} under the heading that names it")
    entry = re.search(rf'href="#{re.escape(owner)}\.{re.escape(member)}">(.*?)</a>', toc, re.S)
    if not entry:
        found.append(f"{page_name}/: On this page does not list {member}")
    elif _text(entry.group(1)).strip() != member:
        found.append(f"{page_name}/: On this page names {member} "
                     f"{_text(entry.group(1)).strip()!r}, not by its own name")
    return found


def main(argv, docstring_of=import_docstring, **expected):
    if len(argv) != 2:
        print(f"usage: {argv[0]} SITE", file=sys.stderr)
        return 2
    found = problems(argv[1], docstring_of, **expected)
    for problem in found:
        print(f"::error::{problem}")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

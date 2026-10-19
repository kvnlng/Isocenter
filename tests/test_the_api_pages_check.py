""".github/scripts/check_api_pages.py, run on fixture pages.

docs/conf.py's `_present_objects` turns napoleon's `Variables` lists into
"Attributes:" tables, drops the `class` prefix, shows DicomSession as
Session and names members in "On this page". The tests environment has
no docutils to run it, so the deploy job checks the built site with this
script (review of #1156: a hook writing every Type cell empty, or
dropping a row it could not split, built green and passed every test).
These tests hold that each such loss fails the script, that a page as
the hook writes it passes, and that docs.yml runs it before the push.

Fixture ids are `pkg.*`: a dotted package name in a test file reads to
tests/test_mutation_probe_targets.py as an import.
"""
import importlib.util
import pathlib
import re

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / ".github" / "scripts" / "check_api_pages.py"
WORKFLOW = REPO / ".github" / "workflows" / "docs.yml"


def _load():
    spec = importlib.util.spec_from_file_location("_check_api_pages", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CHECK = _load()

DOCSTRINGS = {
    "pkg.session.Sess": """The session.

    Attributes:
        store (Store): The graph.
        configuration (Config): The policy, which
            spans two lines.
    """,
    "pkg.entities.Thing": """A thing.

    Attributes:
        name (str): Its name.
        parts (List[Tuple[str, str]]): Its parts.
        count: How many, untyped.
    """,
}
EXPECTED = dict(entity_tables=1,
                known_row=("api/session", "pkg.session.Sess", "configuration", "Config"),
                displayed=("api/session", "pkg.session.Sess", "Session", "ingest"))

_ROWS = {
    "pkg.session.Sess": [("store", "<em>Store</em>", "The graph."),
                         ("configuration", '<a href="#x"><em>Config</em></a>', "The policy.")],
    "pkg.entities.Thing": [("name", "<em>str</em>", "Its name."),
                           ("parts", "<em>List</em><em>[</em><em>Tuple</em><em>[</em><em>str"
                                     "</em><em>, </em><em>str</em><em>]</em><em>]</em>", "Parts."),
                           ("count", "", "How many.")],
}


def _table(rows):
    body = "".join(
        f'<tr class="row-even"><td><p><code class="docutils literal notranslate">'
        f'<span class="pre">{name}</span></code></p></td>\n<td><p>{kind}</p></td>\n'
        f"<td><p>{text}</p></td>\n</tr>\n" for name, kind, text in rows)
    return ('<p class="rubric">Attributes:</p>\n<div class="pst-scrollable-table-container">'
            '<table class="ledger-attributes table">\n<thead>\n<tr class="row-odd">'
            '<th class="head"><p>Name</p></th>\n<th class="head"><p>Type</p></th>\n'
            '<th class="head"><p>Description</p></th>\n</tr>\n</thead>\n<tbody>\n'
            f"{body}</tbody>\n</table>\n</div>\n")


def _class(owner, shown, rows, prefix="", fields=""):
    return (f'<dl class="py class">\n<dt class="sig sig-object py" id="{owner}">\n{prefix}'
            f'<span class="sig-name descname"><span class="pre">{shown}</span></span>'
            f'<span class="sig-paren">(</span><span class="sig-paren">)</span></dt>\n'
            f"<dd><p>Text.</p>\n{fields}{_table(rows)}</dd></dl>\n")


def _toc(entries):
    links = "".join(f'<li><a href="#{anchor}"><code class="docutils literal notranslate">'
                    f'<span class="pre">{text}</span></code></a></li>' for anchor, text in entries)
    return f'<nav id="pst-page-toc-nav" class="page-toc"><ul>{links}</ul></nav>\n'


def _site(tmp_path, *, session=None, entities=None, toc=None, extra=None):
    site = tmp_path / "site"
    pages = {
        "api/session": session if session is not None else
        _class("pkg.session.Sess", "Session", _ROWS["pkg.session.Sess"]),
        "api/entities": entities if entities is not None else
        _class("pkg.entities.Thing", "Thing", _ROWS["pkg.entities.Thing"]),
    }
    pages["api/session"] += toc if toc is not None else _toc(
        [("session", "Session"), ("pkg.session.Sess.ingest", "ingest")])
    for rel, text in {**pages, **(extra or {})}.items():
        (site / rel).mkdir(parents=True, exist_ok=True)
        (site / rel / "index.html").write_text(f"<html><body>{text}</body></html>",
                                               encoding="utf-8")
    return site


def _problems(site):
    return CHECK.problems(site, DOCSTRINGS.get, **EXPECTED)


def test_pages_as_the_hook_writes_them_pass(tmp_path, capsys):
    site = _site(tmp_path)
    assert _problems(site) == []
    assert CHECK.main(["check", str(site)], DOCSTRINGS.get, **EXPECTED) == 0
    assert capsys.readouterr().out == ""


def _fails(site, wanted, capsys):
    found = _problems(site)
    assert any(wanted in problem for problem in found), found
    assert CHECK.main(["check", str(site)], DOCSTRINGS.get, **EXPECTED) == 1
    assert "::error::" in capsys.readouterr().out


def _with(owner, **change):
    """The fixture rows of `owner`, one row changed or dropped."""
    rows = list(_ROWS[owner])
    for name, value in change.items():
        index = next(i for i, row in enumerate(rows) if row[0] == name)
        if value is None:
            del rows[index]
        else:
            rows[index] = value
    return rows


def test_a_type_cell_written_empty_fails(tmp_path, capsys):
    """Review mutant C12: every Type cell empty, so each type vanishes."""
    entities = _class("pkg.entities.Thing", "Thing",
                      [(name, "", text) for name, _, text in _ROWS["pkg.entities.Thing"]])
    _fails(_site(tmp_path, entities=entities), "name's type reads ''", capsys)


def test_the_known_row_without_its_type_fails(tmp_path, capsys):
    session = _class("pkg.session.Sess", "Session",
                     _with("pkg.session.Sess", configuration=("configuration", "", "The policy.")))
    _fails(_site(tmp_path, session=session), "no configuration row typed Config", capsys)


def test_a_row_dropped_from_the_table_fails(tmp_path, capsys):
    """Review mutant C7: an item the split did not recognise is skipped and
    the table is built anyway, one row short of the docstring."""
    entities = _class("pkg.entities.Thing", "Thing", _with("pkg.entities.Thing", parts=None))
    _fails(_site(tmp_path, entities=entities), "has 2 rows; its Attributes section has 3", capsys)


def test_a_row_in_the_wrong_place_fails(tmp_path, capsys):
    rows = _ROWS["pkg.entities.Thing"]
    entities = _class("pkg.entities.Thing", "Thing", [rows[1], rows[0], rows[2]])
    _fails(_site(tmp_path, entities=entities), "is not 'name'", capsys)


def test_a_variables_list_left_on_any_page_fails(tmp_path, capsys):
    """Review mutant C4: no table conversion at all."""
    left = ('<dl class="field-list simple"><dt class="field-odd">Variables'
            '<span class="colon">:</span></dt><dd>x</dd></dl>')
    _fails(_site(tmp_path, extra={"guide": left}), "guide/index.html still has a Variables", capsys)


def test_the_table_placed_before_the_classs_other_fields_fails(tmp_path, capsys):
    """Review mutant C8: the rubric and table inserted before the field
    list that still holds the class's other fields."""
    fields = '<dl class="field-list simple"><dt class="field-odd">Raises</dt><dd>x</dd></dl>\n'
    good = _class("pkg.entities.Thing", "Thing", _ROWS["pkg.entities.Thing"], fields=fields)
    assert _problems(_site(tmp_path / "good", entities=good)) == []
    bad = good.replace(fields + '<p class="rubric">', '<p class="rubric">').replace(
        "</div>\n</dd>", "</div>\n" + fields + "</dd>")
    _fails(_site(tmp_path / "bad", entities=bad), "comes before the class's other fields", capsys)


def test_a_class_prefix_left_fails(tmp_path, capsys):
    """Review mutant C5."""
    prefix = ('<span class="property"><span class="k"><span class="pre">class</span></span>'
              '<span class="w"> </span></span>')
    entities = _class("pkg.entities.Thing", "Thing", _ROWS["pkg.entities.Thing"], prefix=prefix)
    _fails(_site(tmp_path, entities=entities), "with its `class` prefix", capsys)


def test_the_class_under_its_own_name_fails(tmp_path, capsys):
    """Review mutant C11 (and C3): DicomSession not shown as Session."""
    session = _class("pkg.session.Sess", "Sess", _ROWS["pkg.session.Sess"])
    _fails(_site(tmp_path, session=session), "is not shown as Session", capsys)


def test_the_class_listed_under_its_own_heading_fails(tmp_path, capsys):
    """Review mutant C6 (and C1): the class's own "On this page" entry kept."""
    toc = _toc([("session", "Session"), ("pkg.session.Sess", "Session"),
                ("pkg.session.Sess.ingest", "ingest")])
    _fails(_site(tmp_path, toc=toc), "lists pkg.session.Sess under the heading", capsys)


@pytest.mark.parametrize("entries, wanted", [
    ([("session", "Session")], "does not list ingest"),                       # C14
    ([("pkg.session.Sess.ingest", "ingest()")], "names ingest 'ingest()'"),   # C2
    ([("pkg.session.Sess.ingest", "Sess.ingest")], "names ingest 'Sess.ingest'"),  # C13
])
def test_a_member_missing_or_misnamed_in_on_this_page_fails(tmp_path, capsys, entries, wanted):
    _fails(_site(tmp_path, toc=_toc(entries)), wanted, capsys)


def test_a_table_count_other_than_expected_on_entities_fails(tmp_path, capsys):
    two = (_class("pkg.entities.Thing", "Thing", _ROWS["pkg.entities.Thing"]) * 2)
    _fails(_site(tmp_path, entities=two), "api/entities/ has 2 attributes tables, not 1", capsys)


def test_a_docstring_it_cannot_read_fails(tmp_path, capsys):
    session = _class("pkg.session.Gone", "Session", _ROWS["pkg.session.Sess"])
    _fails(_site(tmp_path, session=session), "(it was not found)", capsys)


def test_a_site_with_no_api_pages_fails(tmp_path):
    (tmp_path / "site").mkdir()
    assert CHECK.problems(tmp_path / "site", DOCSTRINGS.get, **EXPECTED)


def test_usage(capsys):
    assert CHECK.main(["check"]) == 2


@pytest.mark.parametrize("docstring, items", [
    (DOCSTRINGS["pkg.entities.Thing"],
     [("name", "str"), ("parts", "List[Tuple[str, str]]"), ("count", "")]),
    (DOCSTRINGS["pkg.session.Sess"], [("store", "Store"), ("configuration", "Config")]),
    ("No section.", []),
    ("Text.\n\nAttributes:\n    a (int): A.\n\nExample:\n    b (int): not an attribute.\n",
     [("a", "int")]),
    (None, []),
])
def test_the_attribute_items_read_from_a_docstring(docstring, items):
    assert CHECK.attribute_items(docstring) == items


def test_the_expected_figures_name_real_rows():
    """The defaults the deploy runs with: the known row is in its class's
    docstring with that type, the displayed class's member is real, and
    api/entities.md documents ENTITY_TABLES classes with an Attributes
    section."""
    page, owner, name, kind = CHECK.KNOWN_ROW
    assert (name, kind) in CHECK.attribute_items(CHECK.import_docstring(owner))
    page, owner, shown, member = CHECK.DISPLAYED
    assert CHECK.import_docstring(f"{owner}.{member}")
    targets = re.findall(r"^\.\.\s+autoclass::\s+(\S+)\s*$",
                         (REPO / "docs" / "api" / "entities.md").read_text(encoding="utf-8"),
                         re.MULTILINE)
    with_section = [t for t in targets if CHECK.attribute_items(CHECK.import_docstring(t))]
    assert len(with_section) == CHECK.ENTITY_TABLES, with_section


def test_the_deploy_runs_the_check_between_the_build_and_the_push():
    steps = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["deploy"]["steps"]
    runs = [step.get("run") or "" for step in steps]
    checking = [i for i, run in enumerate(runs)
                if ".github/scripts/check_api_pages.py" in run]
    assert len(checking) == 1, checking
    step = steps[checking[0]]
    assert step["run"].split() == ["python", ".github/scripts/check_api_pages.py", '"$SITE"']
    building = next(i for i, run in enumerate(runs) if "sphinx-build" in run)
    deploying = next(i for i, run in enumerate(runs) if "docs_deploy.sh" in run)
    assert building < checking[0] < deploying
    assert step["env"]["SITE"] == steps[building]["env"]["SITE"]
    assert not step.get("continue-on-error") and "if" not in step

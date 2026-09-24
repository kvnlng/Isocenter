"""Every docstring the API reference renders must parse as griffe reads it (#565).

`mkdocs build --strict` failed on `main` with 30 griffe warnings, all
from `docs/api/*.md`'s `:::` targets, and the deploy workflow does not
build strict, so the reference rendered wrongly for releases while the
site stayed green. Three shapes, each measured against griffe 2.3.0's
Google parser (`griffe/_internal/docstrings/google.py`) rather than
guessed from the style guide:

**Rule 1 -- one item per `Returns:`/`Yields:` block.** Griffe reads
every line at the block's base indent as another returned value; a
continuation must be indented at *twice* the base indent (a line
between the two draws a "confusing indentation" warning of its own). A
thirteen-line paragraph under `Returns:` rendered as thirteen untyped
"returned value N" rows -- `export`'s did.

**Rule 2 -- the item is typed before its colon, or the def is
annotated.** `mkdocs.yml` sets `returns_named_value: false`, so griffe
splits the item's first line at its first colon and reads the left half
as the type: the canonical Google form (`bool: True if covered.`) this
tree writes everywhere. Without that option the word before the colon is
a *name*, the type comes only from the def's return annotation, and
eight unannotated methods warned "No type or annotation for returned
value 'int'". An item with no colon at all falls back to the return
annotation either way, and warns when there is none. The half before the
colon must also look like a type: griffe would silently render `Note:
...` as a returned value of type `Note`.

**Rule 3 -- every `Args:` entry is `name (type): ...`, or the parameter
is annotated.** Griffe takes the annotation from the signature when the
entry carries no `(type)`, stripping the stars from `**options` to look
it up, and warns when the signature has none. An entry naming a
parameter the def does not have warns too.

The scope is the `:::` lines of `docs/api/*.md`, read at test time, so
a page added to the reference is graded without anyone editing this
file. Private members (`_name`) are what mkdocstrings' default filter
hides and are not graded, except one a page names as its own target
(#27); dunders are graded. That is a superset of what
the site shows, not a copy of it: a page with an explicit `members:`
list renders only those names -- `docs/api/session.md` lists the frozen
surface of `DicomSession` and none of its dunders -- while this grades
every documented member of the target. A red on a method you cannot
find on the site is that case. Fixtures below spell each rule on a
string, so the sweep's verdict and the rules' own behaviour are pinned
separately.

No `scripts/mutation_probe.py` `TARGETS` row: this reads source as text
and imports no package module, the argument
`tests/test_documented_api_exists.py` makes for itself. It also spells
no dotted module name, for the reason that file gives.
"""
import ast
import inspect
import pathlib
import re
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
PACKAGE = REPO / "isocenter"
API_PAGES = REPO / "docs" / "api"

# `::: <package>.<module>[.<Class>]` -- the mkdocstrings directive. (Not
# spelled with a real module here: the targets guard reads this file's
# text for dotted module names, as the module docstring says.)
_DIRECTIVE = re.compile(r"^:::\s+(?P<target>[\w.]+)\s*$", re.MULTILINE)

_RETURNS = re.compile(r"^(?:Returns?|Yields?):\s*$", re.IGNORECASE)
_ARGS = re.compile(r"^(?:Args|Arguments|Params|Parameters):\s*$",
                   re.IGNORECASE)

# What the half before an item's colon may look like when it is a type:
# `bool`, `pd.DataFrame`, `Optional[dict]`, `Dict[str, Any]`,
# `Tuple[int, int]`, `'PhiStatus'`, and PEP 604 unions of those --
# `int | None`, `str | Path`, `Dict[str, int | None]` -- which are the
# ordinary spelling on a 3.12 floor and which griffe reads as the type
# without a warning (measured on griffe 2.3.0 in the review of #637).
# A sentence is none of these, and neither is `np.ndarray or None`:
# `or` is English, and admitting a bare word between two names would
# re-admit the `Note that this is prose:` case below.
#
# Each atom is an atomic group, `(?>...)` (3.11+; the floor is 3.12).
# The bracket class admits `]`, ` ` and `|`, which the separator uses
# too, so `a[b] | a[b] | ... x` parses as one atom or many at every
# `]`, and a near-miss backtracked through all of them: 0.3 s at 22
# atoms, x4 per two more. Committing each atom to its longest match
# drops no string the old pattern admitted -- exhaustively over a
# ten-character alphabet up to length 8, 0 of 111,111,110 differ
# (review of #637) -- and makes the near-miss linear.
_TYPE_ATOM = r"'?[A-Za-z_][\w.]*'?(?:\[[\w.,'\[\] |]+\])?"
_TYPE_SHAPE = re.compile(rf"^(?>{_TYPE_ATOM})(?:\s*\|\s*(?>{_TYPE_ATOM}))*$")

# `name (type): description` / `name: description`, first line of an
# Args entry.
_ARG_ITEM = re.compile(r"^(?P<name>\*{0,2}[A-Za-z_]\w*)\s*"
                       r"(?:\((?P<type>[^)]*)\))?\s*:")


def _rendered_targets(pages_dir=API_PAGES):
    """The dotted targets `docs/api/*.md` hand to mkdocstrings."""
    targets = []
    for page in sorted(pages_dir.glob("*.md")):
        text = page.read_text(encoding="utf-8")
        targets.extend(m.group("target") for m in _DIRECTIVE.finditer(text))
    return targets


def _resolve(target, package=PACKAGE):
    """`(module path, class name or None)` for one `:::` target.

    The longest dotted prefix that names a `.py` file under the package
    is the module; one further segment, if any, is a class in it, and a
    second is one member of that class, returned as `"Class.member"`
    (#27: `docs/api/session.md` renders `_export_dicom` alone). A
    prefix naming a subpackage is its `__init__.py`: the exporter
    registry page renders the `exporters` package itself (#527).
    """
    parts = target.split(".")
    root = package.parent
    for cut in range(len(parts), 0, -1):
        candidate = root.joinpath(*parts[:cut]).with_suffix(".py")
        if not candidate.is_file():
            candidate = root.joinpath(*parts[:cut], "__init__.py")
        if candidate.is_file():
            rest = parts[cut:]
            if len(rest) > 2:
                raise ValueError(f"{target}: nested target not supported")
            return candidate, (".".join(rest) if rest else None)
    raise ValueError(f"{target}: names no module under {package}")


def _is_rendered(name):
    """Whether a member is graded: `_private` is not, `__dunder__` is.

    Modelled on mkdocstrings' default filter, which hides `_private` and
    keeps dunders. Graded, not rendered: a page's explicit `members:`
    list can leave a graded member off the site (the module docstring).
    """
    return not (name.startswith("_") and not name.startswith("__"))


def _documented_nodes(tree, class_name=None):
    """Every rendered def or class with a docstring, in source order."""
    if class_name is not None and "." in class_name:
        # One member, named by the page: rendered whatever the default
        # filter would hide, so graded whatever its name (#27).
        owner, member = class_name.split(".")
        members = [node for cls in tree.body
                   if isinstance(cls, ast.ClassDef) and cls.name == owner
                   for node in cls.body
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and node.name == member]
        if not members:
            raise ValueError(f"member {class_name} not found")
        return [node for node in members
                if ast.get_docstring(node, clean=False)]
    if class_name is None:
        roots = [tree]
    else:
        roots = [node for node in tree.body
                 if isinstance(node, ast.ClassDef) and node.name == class_name]
        if not roots:
            raise ValueError(f"class {class_name} not found")
    found = []

    def walk(node, rendered):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef)):
                visible = rendered and _is_rendered(child.name)
                if visible and ast.get_docstring(child, clean=False):
                    found.append(child)
                # A class's members are rendered; a def's closures are
                # not members of anything, so griffe never sees them.
                if isinstance(child, ast.ClassDef):
                    walk(child, visible)
            elif not isinstance(child, ast.expr):
                walk(child, rendered)

    for root in roots:
        if isinstance(root, ast.ClassDef) and ast.get_docstring(root, clean=False):
            found.append(root)
        walk(root, True)
    return found


def _blocks(doc, title):
    """`(header line index, [item lines...])` for each `title` section.

    Griffe's block reader: base indent from the first non-empty line,
    lines at that indent start a new item, lines at twice that indent
    continue it, anything shallower ends the section.
    """
    lines = inspect.cleandoc(doc).splitlines()
    blocks = []
    i = 0
    while i < len(lines):
        if not title.match(lines[i]):
            i += 1
            continue
        header = i
        i += 1
        while i < len(lines) and not lines[i].strip():
            i += 1
        if i >= len(lines):
            break
        base = len(lines[i]) - len(lines[i].lstrip())
        if base == 0:
            blocks.append((header, base, []))
            continue
        items = []
        while i < len(lines):
            line = lines[i]
            if not line.strip():
                if items:
                    items[-1].append((i, ""))
            elif line.startswith(" " * (base * 2)):
                items[-1].append((i, line))
            elif line.startswith(" " * (base + 1)):
                # Griffe's "confusing indentation": appended but warned.
                items[-1].append((i, line))
                items.append(None)
            elif line.startswith(" " * base):
                items.append([(i, line)])
            else:
                break
            i += 1
        blocks.append((header, base, items))
    return blocks


def _parameters(node):
    """`{name: annotated?}` over every parameter of a def."""
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return {}
    args = node.args
    params = {}
    for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
        params[arg.arg] = arg.annotation is not None
    for arg in (args.vararg, args.kwarg):
        if arg is not None:
            params[arg.arg] = arg.annotation is not None
    return params


def _node_offenders(node, where):
    """The griffe warnings one documented node would draw, as strings."""
    doc = ast.get_docstring(node, clean=False)
    offenders = []
    name = node.name
    annotated = (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and node.returns is not None)
    # `cleandoc` drops a docstring's leading blank lines, so a docstring
    # opening on a newline has its section indexes one short of the
    # source; add them back so the reported line is the `Returns:` line.
    lead = 0
    for raw_line in doc.splitlines():
        if raw_line.strip():
            break
        lead += 1
    origin = node.body[0].lineno + lead

    for header, base, items in _blocks(doc, _RETURNS):
        line = origin + header
        if base == 0 or not items:
            offenders.append(
                f"{where}:{line}: {name}: the Returns/Yields block is "
                "empty, so griffe reads the section title as prose")
            continue
        if None in items:
            offenders.append(
                f"{where}:{line}: {name}: a continuation line under "
                f"Returns/Yields is indented between {base} and "
                f"{base * 2} spaces; griffe warns and guesses")
            items = [item for item in items if item is not None]
        if len(items) > 1:
            offenders.append(
                f"{where}:{line}: {name}: {len(items)} lines at the "
                "Returns/Yields block's base indent -- griffe renders "
                "each as a separate returned value; indent the "
                f"continuation to {base * 2} spaces (rule 1)")
        first = items[0][0][1].strip()
        if ":" not in first:
            if not annotated:
                offenders.append(
                    f"{where}:{line}: {name}: the Returns/Yields item "
                    f"{first[:40]!r} names no type before a colon and the "
                    "def has no return annotation (rule 2)")
        elif not _TYPE_SHAPE.match(first.split(":", 1)[0].strip()):
            offenders.append(
                f"{where}:{line}: {name}: {first.split(':', 1)[0]!r} is "
                "read as the returned type and is not one (rule 2)")

    params = _parameters(node)
    for header, base, items in _blocks(doc, _ARGS):
        line = origin + header
        for item in items:
            if item is None:
                continue
            first = item[0][1].strip()
            match = _ARG_ITEM.match(first)
            if not match:
                offenders.append(
                    f"{where}:{line}: {name}: Args entry {first[:40]!r} "
                    "is not `name (type): description` (rule 3)")
                continue
            param = match.group("name").lstrip("*")
            if param not in params:
                offenders.append(
                    f"{where}:{line}: {name}: Args names {param!r}, "
                    "which the def does not take (rule 3)")
            elif match.group("type") is None and not params[param]:
                offenders.append(
                    f"{where}:{line}: {name}: Args entry {param!r} has "
                    "no (type) and the parameter is unannotated (rule 3)")
    return offenders


def check_source(source, where="<string>", class_name=None):
    """`(offenders, graded)` over one module's rendered docstrings."""
    tree = ast.parse(source, where)
    offenders = []
    nodes = _documented_nodes(tree, class_name)
    for node in nodes:
        offenders.extend(_node_offenders(node, where))
    return offenders, len(nodes)


def check_rendered_scope(pages_dir=API_PAGES, package=PACKAGE):
    """`(offenders, graded, targets)` over every `:::` target."""
    offenders = []
    graded = 0
    targets = _rendered_targets(pages_dir)
    for target in targets:
        path, class_name = _resolve(target, package)
        found, count = check_source(
            path.read_text(encoding="utf-8"),
            path.relative_to(package.parent).as_posix(), class_name)
        offenders.extend(found)
        graded += count
    return offenders, graded, targets


def test_every_rendered_docstring_parses_as_one_typed_value_per_block():
    """The sweep: red on `main` at 30 griffe warnings (#565)."""
    offenders, graded, targets = check_rendered_scope()

    # #299's precedent: a scope that resolves to nothing is a broken
    # walk, not a clean reference.
    assert len(targets) >= 9, targets
    assert graded >= 100, (
        f"only {graded} documented members found under the API pages' "
        "targets; the walk is broken and this would pass vacuously")

    assert not offenders, (
        "these docstrings render wrongly in the API reference and fail "
        "`mkdocs build --strict` (#565):\n    " + "\n    ".join(offenders))


# --- The rules on their own, one fixture each ------------------------------

def _one(source, **kw):
    offenders, graded = check_source(source, "fixture.py", **kw)
    assert graded == 1, graded
    return offenders


def test_a_second_line_at_the_base_indent_is_a_second_returned_value():
    """Rule 1, positive: the mutant that accepts two items dies here."""
    offenders = _one(
        "def f() -> int:\n"
        '    """Summary.\n\n'
        "    Returns:\n"
        "        int: how many.\n"
        "        Counted twice on Tuesdays.\n"
        '    """\n')
    assert len(offenders) == 1, offenders
    assert "2 lines at the Returns/Yields block's base indent" in offenders[0]


def test_a_continuation_indented_twice_the_base_is_the_same_item():
    """Rule 1, negative: the griffe continuation shape is clean."""
    assert _one(
        "def f() -> int:\n"
        '    """Summary.\n\n'
        "    Returns:\n"
        "        int: how many,\n"
        "            counted twice on Tuesdays.\n\n"
        "            A second paragraph, still the same item.\n"
        '    """\n') == []


def test_a_continuation_between_the_two_indents_is_flagged():
    """Griffe warns "confusing indentation" and guesses; that is red."""
    offenders = _one(
        "def f() -> int:\n"
        '    """Summary.\n\n'
        "    Returns:\n"
        "        int: how many,\n"
        "          counted twice.\n"
        '    """\n')
    assert len(offenders) == 1, offenders
    assert "indented between 4 and 8 spaces" in offenders[0]


def test_an_untyped_returns_item_needs_a_return_annotation():
    """Rule 2: no colon and no annotation warns; either one is enough."""
    untyped = ('    """Summary.\n\n'
               "    Returns:\n"
               "        How many.\n"
               '    """\n')
    assert _one("def f() -> int:\n" + untyped) == []
    offenders = _one("def f():\n" + untyped)
    assert len(offenders) == 1, offenders
    assert "names no type before a colon" in offenders[0]
    # `Type: description` needs no annotation at all.
    assert _one("def f():\n"
                '    """Summary.\n\n'
                "    Returns:\n"
                "        Optional[Dict[str, Any]]: the table, or None.\n"
                '    """\n') == []


def test_a_sentence_before_the_colon_is_not_a_type():
    """Rule 2's shape check: `Note: ...` would render as a type `Note`."""
    offenders = _one("def f():\n"
                     '    """Summary.\n\n'
                     "    Returns:\n"
                     "        Note that this is prose: it is.\n"
                     '    """\n')
    assert len(offenders) == 1, offenders
    assert "is read as the returned type and is not one" in offenders[0]


def test_a_pep_604_union_before_the_colon_is_a_type():
    """Rule 2's shape admits `|`, and only `|` (review of #637).

    Griffe reads `int | None: the count.` as a returned value of type
    `int | None` with no warning; a guard red on that is red on the
    spelling a 3.12-floor maintainer writes first. `or` is not an
    annotation, so `np.ndarray or None:` stays an offender.
    """
    def returns(item):
        return _one("def f():\n"
                    '    """Summary.\n\n'
                    "    Returns:\n"
                    f"        {item}\n"
                    '    """\n')

    assert returns("int | None: the count, or None.") == []
    assert returns("str | Path: where it went.") == []
    assert returns("Dict[str, int | None]: per tag.") == []
    offenders = returns("np.ndarray or None: the frame.")
    assert len(offenders) == 1, offenders
    assert ("'np.ndarray or None' is read as the returned type and is not "
            "one") in offenders[0], offenders


def test_a_near_miss_union_is_rejected_in_linear_time():
    """A malformed union fails fast, not by backtracking every parse.

    `_TYPE_SHAPE`'s comment has the ambiguity. The 22-atom string comes
    first because it is the one a regression can finish: without the
    atomic groups it takes about 340 ms, where the 10k-character one
    would not return at all. 50 ms is a budget a loaded CI box meets
    with room; the fixed pattern takes microseconds on both.
    """
    for atoms in (22, 1430):
        text = " | ".join(["a[b]"] * atoms) + " x"
        start = time.perf_counter()
        matched = _TYPE_SHAPE.match(text)
        elapsed = time.perf_counter() - start
        assert matched is None, text[:80]
        assert elapsed < 0.05, (
            f"{len(text)} characters took {elapsed * 1e3:.0f} ms to reject")


def test_an_untyped_args_entry_needs_a_parameter_annotation():
    """Rule 3, both arms, and the star-stripped `**options` lookup."""
    doc = ('    """Summary.\n\n'
           "    Args:\n"
           "        folder: where.\n"
           "        **options: the rest.\n"
           '    """\n')
    assert _one("def f(folder: str, **options: dict):\n" + doc) == []
    offenders = _one("def f(folder, **options):\n" + doc)
    assert len(offenders) == 2, offenders
    assert "'folder' has no (type)" in offenders[0]
    assert "'options' has no (type)" in offenders[1]
    assert _one("def f(folder, **options):\n"
                '    """Summary.\n\n'
                "    Args:\n"
                "        folder (str): where.\n"
                "        **options (dict): the rest.\n"
                '    """\n') == []


def test_an_args_entry_naming_no_parameter_is_flagged():
    offenders = _one("def f(folder: str):\n"
                     '    """Summary.\n\n'
                     "    Args:\n"
                     "        folder (str): where.\n"
                     "        fodler (str): a typo.\n"
                     '    """\n')
    assert len(offenders) == 1, offenders
    assert "names 'fodler', which the def does not take" in offenders[0]


def test_private_members_are_not_graded_and_dunders_are():
    """mkdocstrings' default filter hides `_name`; this must match it."""
    source = ("class C:\n"
              '    """A class."""\n'
              "    def _hidden(self):\n"
              '        """Returns:\n            two.\n            items.\n        """\n'
              "    def __call__(self):\n"
              '        """Returns:\n            two.\n            items.\n        """\n')
    offenders, graded = check_source(source, "fixture.py")
    assert graded == 2, graded
    assert len(offenders) == 1 and "__call__" in offenders[0], offenders


def test_the_scope_is_read_from_the_api_pages(tmp_path):
    """A page's `:::` line is the scope; nothing here hardcodes a module."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "mod.py").write_text(
        "class K:\n"
        '    """A class."""\n'
        "    def m(self):\n"
        '        """Returns:\n            two.\n            items.\n        """\n'
        "def loose():\n"
        '    """Returns:\n        two.\n        items.\n    """\n',
        encoding="utf-8")
    pages = tmp_path / "api"
    pages.mkdir()
    (pages / "k.md").write_text("# K\n\n::: pkg.mod.K\n", encoding="utf-8")

    offenders, graded, targets = check_rendered_scope(pages, pkg)

    assert targets == ["pkg.mod.K"]
    assert graded == 2, graded
    assert len(offenders) == 1 and " m: " in offenders[0], offenders

    (pages / "mod.md").write_text("::: pkg.mod\n", encoding="utf-8")
    offenders, graded, _ = check_rendered_scope(pages, pkg)
    assert graded == 5, graded
    assert any(" loose: " in o for o in offenders), offenders


def test_a_method_target_grades_that_method_even_when_private(tmp_path):
    """`::: pkg.mod.K._m` renders one method, and that method is graded (#27).

    `docs/api/session.md` renders `_export_dicom` on purpose: its docstring
    is the one definition of the `dicom` export options. A page that names
    a member explicitly shows it whatever the default filter would hide, so
    the sweep grades it -- and only it, not the rest of its class. Before
    #27 a two-segment target raised "nested target not supported".
    """
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "mod.py").write_text(
        "class K:\n"
        '    """A class."""\n'
        "    def m(self):\n"
        '        """Returns:\n            two.\n            items.\n        """\n'
        "    def _m(self):\n"
        '        """Returns:\n            two.\n            items.\n        """\n',
        encoding="utf-8")
    pages = tmp_path / "api"
    pages.mkdir()
    (pages / "k.md").write_text("::: pkg.mod.K._m\n", encoding="utf-8")

    offenders, graded, targets = check_rendered_scope(pages, pkg)

    assert targets == ["pkg.mod.K._m"]
    assert graded == 1, graded
    assert len(offenders) == 1 and " _m: " in offenders[0], offenders

    (pages / "k.md").write_text("::: pkg.mod.K.absent\n", encoding="utf-8")
    try:
        check_rendered_scope(pages, pkg)
    except ValueError as error:
        assert "absent" in str(error)
    else:
        raise AssertionError("a target naming no member resolved")

"""Every docstring in the package must render as napoleon and docutils read it.

The API reference is built by Sphinx: autodoc imports each target,
napoleon rewrites its Google sections (`Args:`, `Returns:`, `Raises:`)
into reST field lists, and docutils parses the result as reST. Most
ways a docstring goes wrong there draw no warning even under
`sphinx-build -W`: the page just renders a definition list, a block
quote or a raw backtick. So the shapes are graded here, statically,
from the source text. The test imports neither Sphinx nor docutils;
the rules were measured against Sphinx 9.1's napoleon and docutils
0.22, not guessed from the style guides.

**Scope (owner rulings D3 and S8, 2026-10-18).** R1 and R4 to R7
grade every docstring in the package: module, class, def, private
helper, dunder and closure alike, whether or not a page renders it. A
docstring a page does not show today is one a page may show tomorrow,
and one rule for the whole tree is one a contributor can learn. R2 and
R3 grade only what the API pages render (the targets their
`.. auto*::` lines name, by autodoc's default member filter), because
under napoleon they are questions about the rendered page and not
about the source: an untyped `Args:` entry renders without a type and
draws nothing.

**R1 -- a `Returns:`/`Yields:` item continues at its own indent.**
Napoleon reads the whole block as one item, dedents it, and hands the
text after the first colon to docutils as the field body. A
continuation at the item's indent is the same paragraph. One indented
deeper is a definition list (directly under the first line) or a block
quote (after a blank line), with no warning: `export`'s "dicom returns
an" ran into a bold "ExportSummary; wfdb returns ..." term that way.
This is the inverse of the griffe rule (#565), which wanted the
continuation at twice the base indent; no shape serves both, and the
site moved to Sphinx. An empty block is red too.

**R2 -- the item is typed before its colon, or the def is
annotated.** Napoleon splits the item's first line at its first colon
and renders the left half as the return type, so the left half must
look like a type: `Note that this is prose: it is.` would render a
type `Note that this is prose`.

**R3 -- every `Args:` entry is `name (type): ...`, or the parameter
is annotated, and names a parameter the def takes.**

**R4 -- every inline literal and strong span closes where docutils
closes it, and holds no other markup.** docutils pairs spans left to
right within a text block. A start-string must follow whitespace, the
block's start or opening punctuation; its end-string must follow a
non-space character and be followed by whitespace, the block's end or
closing punctuation (`docutils.utils.punctuation_chars`). So
`` `RuntimeError`s `` does not close at its second backtick: docutils
searches on, and either warns or swallows the text up to a later
backtick into one span. A backslash escapes the next character in
reST, so `` `\\` `` never closes either, and one inside a span that
does close is dropped (`'RGB\\t'` renders as 'RGBt'): a backslash goes
in a double-backtick literal, which does no escape processing. reST does not nest inline
markup, so a backtick inside `**strong**` is shown as a backtick (S9).
A lone backtick that opens nothing is shown as itself, and is red
too. A `**` that cannot open a span (`x ** 2`) is plain text and is
not graded.

**R5 -- no Markdown code fence.** reST shows a ```` ``` ```` fence as
text. Use a doctest or a `::` literal block.

**R6 -- no line starting with `#`** outside a literal block or doctest,
except reST's auto-numbered list item (`#. `).
Python-Markdown read `#1108). When ...` as a heading (mkdocstrings gave
it an anchor), and a `# Heading` is Markdown that reST shows as text;
neither is what the author meant on either builder.

**R7 -- no Markdown link** outside code. mkdocstrings resolved
`[ExportSummary][...]` to a link; reST shows the brackets. A role
(`:class:`) does the same job under Sphinx.

Literal blocks (after a `::` paragraph), doctest blocks and the
contents of a fence are code and are not graded by R4, R6 or R7. Fixtures
below spell each rule on a red string and a clean one, so the sweep's
verdict and the rules' own behaviour are pinned separately.

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
import unicodedata

REPO = pathlib.Path(__file__).resolve().parent.parent
PACKAGE = REPO / "isocenter"
API_PAGES = REPO / "docs" / "api"

# The API pages' targets, in both spellings the branch carries: the
# mkdocstrings line `::: <package>.<module>[.<Class>]`, and an autodoc
# directive inside an `{eval-rst}` fence. The `:::` reader goes with the
# last `:::` line. (Not spelled with a real module here: the targets
# guard reads this file's text for dotted module names.)
_DIRECTIVE = re.compile(r"^:::\s+(?P<target>[\w.]+)\s*$", re.MULTILINE)
_EVAL_RST = re.compile(r"^```\{eval-rst\}[^\n]*\n(?P<body>.*?)^```[ \t]*$",
                       re.MULTILINE | re.DOTALL)
_AUTO_DIRECTIVE = re.compile(
    r"^[ \t]*\.\.[ \t]+auto(?:class|module|function|exception|data|method"
    r"|attribute)::[ \t]+(?P<target>[\w.]+)[ \t]*$", re.MULTILINE)

_RETURNS = re.compile(r"^(?:Returns?|Yields?):\s*$", re.IGNORECASE)
_ARGS = re.compile(r"^(?:Args|Arguments|Params|Parameters):\s*$",
                   re.IGNORECASE)

# Napoleon's sections whose body is a list of `name: description`
# fields, each its own field body (so a span cannot pair across two of
# them). `Returns:`/`Yields:` are one item and are not here.
_FIELD_SECTION = re.compile(
    r"^(?:Args|Arguments|Parameters|Params|Keyword Args|Keyword Arguments"
    r"|Other Parameters|Attributes|Raises?|Warns|Receives?|Methods)"
    r":\s*$", re.IGNORECASE)
# Any napoleon section header: it ends the text block above it.
_SECTION = re.compile(
    r"^(?:Args|Arguments|Attention|Attributes|Caution|Danger|Error"
    r"|Examples?|Hint|Important|Keyword Args|Keyword Arguments|Methods"
    r"|Notes?|Other Parameters|Parameters|Params|Receives?|Returns?"
    r"|Raises?|References|See Also|Tip|Todo|Warnings?|Warns|Yields?)"
    r":\s*$", re.IGNORECASE)
_LIST_ITEM = re.compile(r"^(?:[-*+]|\d+[.)]|#\.)\s")

# What the half before an item's colon may look like when it is a type:
# `bool`, `pd.DataFrame`, `Optional[dict]`, `Dict[str, Any]`,
# `Tuple[int, int]`, `'PhiStatus'`, and PEP 604 unions of those --
# `int | None`, `str | Path`, `Dict[str, int | None]` -- which are the
# ordinary spelling on a 3.12 floor. A sentence is none of these, and
# neither is `np.ndarray or None`: `or` is English, and admitting a bare
# word between two names would re-admit the `Note that this is prose:`
# case below.
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

# R4's punctuation, from `docutils.utils.punctuation_chars` (0.22):
# ASCII spelled out, the rest by Unicode category, which is how that
# module builds its tables.
_START_PREFIX_ASCII = set("\"'(<[{-/:")
_END_SUFFIX_ASCII = set("\\.,;!?-/:\"')>]}")
_START_PREFIX_CATEGORIES = {"Ps", "Pi", "Pf", "Pd", "Po"}
_END_SUFFIX_CATEGORIES = {"Pe", "Pi", "Pf", "Pd", "Po"}
# An interpreted-text end-string may carry a role (`x`:role:) or a
# reference suffix (`x`_, `x`__) before the suffix proper.
_INTERPRETED_TAIL = re.compile(r":[\w.+-]+:|__?")
_TOKEN = re.compile(r"``|`|\*\*")
# R7: `[text][target]` (mkdocstrings' cross-reference) and `[text](url)`,
# read with code spans blanked to the same length.
_MARKDOWN_LINK = re.compile(r"\[[^\]\n]+\](?:\[[\w.]*\]|\([^)\s]+\))")
_CODE_SPAN = re.compile(r"``.+?``|`[^`]*`", re.DOTALL)


def _blank(match):
    return " " * len(match.group())


def _rendered_targets(pages_dir=API_PAGES):
    """The dotted targets `docs/api/*.md` hand to the API builder."""
    targets = []
    for page in sorted(pages_dir.glob("*.md")):
        text = page.read_text(encoding="utf-8")
        targets.extend(m.group("target") for m in _DIRECTIVE.finditer(text))
        for block in _EVAL_RST.finditer(text):
            targets.extend(m.group("target") for m in
                           _AUTO_DIRECTIVE.finditer(block.group("body")))
    return targets


def _resolve(target, package=PACKAGE):
    """`(module path, class name or None)` for one target.

    The longest dotted prefix that names a `.py` file under the package
    is the module; one further segment, if any, is a class in it (or a
    module-level function or constant, which `_documented_nodes`
    resolves when no class has the name), and a
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
    """Whether R2/R3 grade a member: `_private` is not, `__dunder__` is.

    Modelled on the API builder's default member filter, which hides
    `_private`. Graded, not rendered: a page's explicit member list can
    leave a graded member off the site.
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
            # A module-level function or constant named by the page
            # (`docs/api/entities.md` renders each tier-2 helper on its
            # own): the function is graded, and a constant has no
            # docstring to grade.
            functions = [node for node in tree.body
                         if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                         and node.name == class_name]
            if functions:
                return [node for node in functions
                        if ast.get_docstring(node, clean=False)]
            if any(isinstance(node, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == class_name
                           for t in node.targets)
                   or isinstance(node, ast.AnnAssign)
                   and isinstance(node.target, ast.Name)
                   and node.target.id == class_name
                   for node in tree.body):
                return []
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
                # not members of anything, so autodoc never sees them.
                if isinstance(child, ast.ClassDef):
                    walk(child, visible)
            elif not isinstance(child, ast.expr):
                walk(child, rendered)

    for root in roots:
        if isinstance(root, ast.ClassDef) and ast.get_docstring(root, clean=False):
            found.append(root)
        walk(root, True)
    return found


def _every_documented_node(tree):
    """Every module, class and def with a docstring, closures included."""
    return [node for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef))
            and ast.get_docstring(node, clean=False)]


def _indent(line):
    return len(line) - len(line.lstrip())


def _blocks(lines, title):
    """`(header index, base indent, [(index, line), ...])` per section.

    Napoleon's reader: the section runs from the line after its header
    to the first non-blank line indented no deeper than the header, and
    its base indent is that of its first non-blank line. Blank lines
    inside it are kept as `(index, "")`.
    """
    blocks = []
    i = 0
    while i < len(lines):
        if not title.match(lines[i].strip()):
            i += 1
            continue
        header = i
        header_indent = _indent(lines[i])
        i += 1
        body = []
        while i < len(lines) and (not lines[i].strip()
                                  or _indent(lines[i]) > header_indent):
            body.append((i, lines[i] if lines[i].strip() else ""))
            i += 1
        while body and not body[-1][1]:
            body.pop()
        base = _indent(body[0][1]) if body else header_indent
        blocks.append((header, base, body))
    return blocks


def _items(body, base):
    """A field section's items: each starts at `base`, the rest continue."""
    items = []
    for index, line in body:
        if line and _indent(line) == base:
            items.append([(index, line)])
        elif items:
            items[-1].append((index, line))
    return items


def _parameters(node):
    """`{name: annotated?}` over every parameter of a def.

    A class's `Args:` documents its `__init__` (autodoc's
    `class-doc-from: both`), so a class answers with its `__init__`'s
    parameters, `self` left out.
    """
    if isinstance(node, ast.ClassDef):
        init = next((child for child in node.body
                     if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and child.name == "__init__"), None)
        params = _parameters(init) if init is not None else {}
        params.pop("self", None)
        return params
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


# --- R4, R5, R6: the text docutils reads ------------------------------------

def _code_lines(lines):
    """Indexes of lines that are code, not text: R4 and R6 skip them.

    A fence's lines (R5 reports the fence itself), a doctest block (a
    paragraph opening with `>>>`), and a literal block (the lines after
    a paragraph ending `::` that are indented deeper than it).
    """
    code = set()
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith("```"):
            start = i
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                i += 1
            code.update(range(start, min(i + 1, len(lines))))
            i += 1
            continue
        if stripped.startswith(">>>"):
            while i < len(lines) and lines[i].strip():
                code.add(i)
                i += 1
            continue
        if stripped.endswith("::"):
            own = _indent(lines[i])
            i += 1
            while i < len(lines) and (not lines[i].strip()
                                      or _indent(lines[i]) > own):
                if lines[i].strip():
                    code.add(i)
                i += 1
            continue
        i += 1
    return code


def _text_blocks(lines, code):
    """Runs of text lines docutils pairs inline markup within.

    A block ends at a blank line or code, before a section header, a
    list item that follows another, a line indented shallower than the
    one above it, and
    before each new field of a field section (`Args:`, `Raises:`, ...),
    since napoleon gives every field its own body. Each block is a list
    of `(index, text)`; a field's leading `*`/`**` is dropped, because
    napoleon escapes it.
    """
    blocks = []
    current = []
    field_base = None
    field_header_indent = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or index in code:
            if current:
                blocks.append(current)
                current = []
            continue
        indent = _indent(line)
        if field_base is not None and indent <= field_header_indent:
            field_base = None
        if _SECTION.match(stripped):
            if current:
                blocks.append(current)
                current = []
            if _FIELD_SECTION.match(stripped):
                field_base = -1
                field_header_indent = indent
            continue
        if field_base == -1:
            field_base = indent
        new_field = field_base is not None and indent == field_base
        text = stripped
        if new_field:
            text = text.lstrip("*")
        # A list item ends the item above it, but not a paragraph: with
        # no blank line between, `0301) NO` is the paragraph's text.
        next_item = (current and _LIST_ITEM.match(stripped)
                     and _LIST_ITEM.match(lines[current[0][0]].strip()))
        if current and (new_field or next_item
                        or indent < _indent(lines[current[-1][0]])):
            blocks.append(current)
            current = []
        current.append((index, text))
    if current:
        blocks.append(current)
    return blocks


def _is_start_prefix(char):
    if char is None or char.isspace():
        return True
    if char.isascii():
        return char in _START_PREFIX_ASCII
    return unicodedata.category(char) in _START_PREFIX_CATEGORIES


def _is_end_suffix(char):
    if char is None or char.isspace():
        return True
    if char.isascii():
        return char in _END_SUFFIX_ASCII
    return unicodedata.category(char) in _END_SUFFIX_CATEGORIES


def _char(text, at):
    return text[at] if 0 <= at < len(text) else None


def _escaped(text, at):
    """Whether `text[at]` follows an odd run of backslashes."""
    run = 0
    while at - run - 1 >= 0 and text[at - run - 1] == "\\":
        run += 1
    return run % 2 == 1


def _closes(text, at, token):
    """Whether `token` at `at` is a valid end-string, and where it ends."""
    before = _char(text, at - 1)
    if before is None or before.isspace():
        return False, at + len(token)
    if token != "``" and _escaped(text, at):
        return False, at + len(token)
    end = at + len(token)
    if token == "`":
        tail = _INTERPRETED_TAIL.match(text, end)
        if tail:
            end = tail.end()
    return _is_end_suffix(_char(text, end)), end


def _span_offenders(text):
    """`(offset, message)` for each span R4 refuses in one text block."""
    offenders = []
    i = 0
    while True:
        match = _TOKEN.search(text, i)
        if match is None:
            return offenders
        token, start = match.group(), match.start()
        opens = (_is_start_prefix(_char(text, start - 1))
                 and _char(text, start + len(token)) is not None
                 and not _char(text, start + len(token)).isspace())
        if token == "**":
            if not opens:
                i = start + 2
                continue
        elif token == "`" and _escaped(text, start):
            i = start + 1
            continue
        elif not opens:
            offenders.append((start, f"{token!r} opens no span here and "
                                     "is shown as itself"))
            i = start + len(token)
            continue
        close = text.find(token, start + len(token))
        if close == -1:
            offenders.append((start, f"{token!r} is never closed"))
            return offenders
        valid, end = _closes(text, close, token)
        if not valid:
            following = text[close:close + len(token) + 1]
            offenders.append((start, f"{token!r} span does not close at "
                                     f"{following!r}: docutils searches on "
                                     "past it"))
            i = close + len(token)
            continue
        if token == "`" and "\\" in text[start + 1:close]:
            offenders.append((start, "a backslash inside a single-backtick "
                                     "span is read as an escape and "
                                     "dropped; use a double-backtick "
                                     "literal"))
        if token == "**" and "`" in text[start + 2:close]:
            offenders.append((start, "a backtick inside a '**' span is "
                                     "shown as a backtick: reST does not "
                                     "nest inline markup"))
        i = end


def _markup_offenders(lines):
    """`(line index, message)` for R4 to R7 over cleandoc'd lines."""
    offenders = []
    code = _code_lines(lines)
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("```"):
            # Each fence line, opening and closing, is one offender.
            offenders.append((index, "a Markdown code fence, which "
                                     "reST shows as text; use a doctest "
                                     "or a `::` literal block (R5)"))
        elif (index not in code and stripped.startswith("#")
              and not stripped.startswith("#. ")):
            # `#. ` is reST's auto-numbered list item, not a heading.
            offenders.append((index, f"the line starts with "
                                     f"{stripped[:12]!r}: Markdown reads "
                                     "a heading there (R6)"))
    for block in _text_blocks(lines, code):
        text = "\n".join(line for _, line in block)
        starts = []
        offset = 0
        for index, line in block:
            starts.append((offset, index))
            offset += len(line) + 1
        for at, message in _span_offenders(text):
            index = max(i for o, i in starts if o <= at)
            offenders.append((index, f"{message} (R4)"))
        for link in _MARKDOWN_LINK.finditer(_CODE_SPAN.sub(_blank, text)):
            index = max(i for o, i in starts if o <= link.start())
            offenders.append((index, f"{link.group()!r} is a Markdown "
                                     "link, which reST shows as text; use "
                                     "a role (`:class:`) or a reST link "
                                     "(R7)"))
    return offenders


# --- One node ----------------------------------------------------------------

def _origin(node, doc):
    """The source line of the docstring's first cleandoc'd line."""
    # `cleandoc` drops a docstring's leading blank lines, so a docstring
    # opening on a newline has its indexes short of the source; add them
    # back so the reported line is the line itself.
    lead = 0
    for raw_line in doc.splitlines():
        if raw_line.strip():
            break
        lead += 1
    return node.body[0].lineno + lead


def _node_name(node):
    return getattr(node, "name", "<module>")


def _node_offenders(node, where):
    """R1 and R4 to R7 for one documented node, as strings."""
    doc = ast.get_docstring(node, clean=False)
    lines = inspect.cleandoc(doc).splitlines()
    origin = _origin(node, doc)
    name = _node_name(node)
    offenders = []
    for header, base, body in _blocks(lines, _RETURNS):
        if not body:
            offenders.append(
                f"{where}:{origin + header}: {name}: the Returns/Yields "
                "block is empty, so napoleon renders nothing under it (R1)")
            continue
        for index, line in body:
            if line and _indent(line) != base:
                offenders.append(
                    f"{where}:{origin + index}: {name}: a Returns/Yields "
                    f"line is indented {_indent(line)} spaces where the "
                    f"item sits at {base}; napoleon renders it as a "
                    "definition list or block quote -- continue at the "
                    "item's own indent (R1)")
    for index, message in _markup_offenders(lines):
        offenders.append(f"{where}:{origin + index}: {name}: {message}")
    return offenders


def _typed_offenders(node, where):
    """R2 and R3 for one rendered node, as strings."""
    doc = ast.get_docstring(node, clean=False)
    lines = inspect.cleandoc(doc).splitlines()
    origin = _origin(node, doc)
    name = _node_name(node)
    offenders = []
    annotated = (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and node.returns is not None)
    for header, _base, body in _blocks(lines, _RETURNS):
        if not body:
            continue
        first = body[0][1].strip()
        line = origin + header
        if ":" not in first:
            if not annotated:
                offenders.append(
                    f"{where}:{line}: {name}: the Returns/Yields item "
                    f"{first[:40]!r} names no type before a colon and the "
                    "def has no return annotation (R2)")
        elif not _TYPE_SHAPE.match(first.split(":", 1)[0].strip()):
            offenders.append(
                f"{where}:{line}: {name}: {first.split(':', 1)[0]!r} is "
                "read as the returned type and is not one (R2)")

    params = _parameters(node)
    for header, base, body in _blocks(lines, _ARGS):
        line = origin + header
        for item in _items(body, base):
            first = item[0][1].strip()
            match = _ARG_ITEM.match(first)
            if not match:
                offenders.append(
                    f"{where}:{line}: {name}: Args entry {first[:40]!r} "
                    "is not `name (type): description` (R3)")
                continue
            param = match.group("name").lstrip("*")
            if param not in params:
                offenders.append(
                    f"{where}:{line}: {name}: Args names {param!r}, "
                    "which the def does not take (R3)")
            elif match.group("type") is None and not params[param]:
                offenders.append(
                    f"{where}:{line}: {name}: Args entry {param!r} has "
                    "no (type) and the parameter is unannotated (R3)")
    return offenders


# --- The two scopes ----------------------------------------------------------

def check_source(source, where="<string>"):
    """`(offenders, graded)`: R1 and R4 to R7 over every docstring."""
    tree = ast.parse(source, where)
    offenders = []
    nodes = _every_documented_node(tree)
    for node in nodes:
        offenders.extend(_node_offenders(node, where))
    return offenders, len(nodes)


def check_package(package=PACKAGE):
    """`(offenders, graded, files)` over every `.py` file in the package."""
    offenders = []
    graded = 0
    files = sorted(package.rglob("*.py"))
    for path in files:
        found, count = check_source(
            path.read_text(encoding="utf-8"),
            path.relative_to(package.parent).as_posix())
        offenders.extend(found)
        graded += count
    return offenders, graded, files


def check_typed(source, where="<string>", class_name=None):
    """`(offenders, graded)`: R2 and R3 over one target's rendered nodes."""
    tree = ast.parse(source, where)
    offenders = []
    nodes = _documented_nodes(tree, class_name)
    for node in nodes:
        offenders.extend(_typed_offenders(node, where))
    return offenders, len(nodes)


def check_rendered_scope(pages_dir=API_PAGES, package=PACKAGE):
    """`(offenders, graded, targets)`: R2 and R3 over every page target."""
    offenders = []
    graded = 0
    targets = _rendered_targets(pages_dir)
    for target in targets:
        path, class_name = _resolve(target, package)
        found, count = check_typed(
            path.read_text(encoding="utf-8"),
            path.relative_to(package.parent).as_posix(), class_name)
        offenders.extend(found)
        graded += count
    return offenders, graded, targets


def test_every_docstring_in_the_package_renders_under_napoleon():
    """The sweep, R1 and R4 to R7, over the whole package (D3)."""
    offenders, graded, files = check_package()

    # #299's precedent: a scope that resolves to nothing is a broken
    # walk, not a clean package.
    assert len(files) >= 40, files
    assert graded >= 1000, (
        f"only {graded} docstrings found in the package; the walk is "
        "broken and this would pass vacuously")

    assert not offenders, (
        "these docstrings render wrongly under napoleon and docutils, "
        "mostly with no warning at all:\n    " + "\n    ".join(offenders))


def test_every_rendered_docstring_is_typed_where_the_page_reads_a_type():
    """R2 and R3 over what the API pages render."""
    offenders, graded, targets = check_rendered_scope()

    assert len(targets) >= 9, targets
    assert graded >= 100, (
        f"only {graded} documented members found under the API pages' "
        "targets; the walk is broken and this would pass vacuously")

    assert not offenders, (
        "these docstrings render a wrong or missing type on the API "
        "pages:\n    " + "\n    ".join(offenders))


# --- The rules on their own, one fixture each ------------------------------

def _one(source):
    offenders, graded = check_source(source, "fixture.py")
    assert graded == 1, graded
    return offenders


def _typed(source, **kw):
    offenders, graded = check_typed(source, "fixture.py", **kw)
    assert graded == 1, graded
    return offenders


def test_a_returns_continuation_at_the_item_indent_is_clean():
    """R1, negative: the napoleon shape, a paragraph after a blank too."""
    assert _one(
        "def f() -> int:\n"
        '    """Summary.\n\n'
        "    Returns:\n"
        "        int: how many,\n"
        "        counted twice on Tuesdays.\n\n"
        "        A second paragraph, still the same item.\n\n"
        "    Raises:\n"
        "        ValueError: when the count is negative, which\n"
        "            it never is.\n"
        '    """\n') == []


def test_a_returns_continuation_indented_deeper_is_red():
    """R1, positive: the griffe shape, and a line between the two."""
    offenders = _one(
        "def f() -> int:\n"
        '    """Summary.\n\n'
        "    Returns:\n"
        "        int: how many,\n"
        "            counted twice on Tuesdays.\n"
        "          And this.\n"
        '    """\n')
    assert len(offenders) == 2, offenders
    assert "fixture.py:6:" in offenders[0], offenders
    assert "indented 8 spaces where the item sits at 4" in offenders[0]
    assert "fixture.py:7:" in offenders[1], offenders
    assert "indented 6 spaces" in offenders[1]
    # A Yields block is the same block.
    offenders = _one(
        "def f():\n"
        '    """Summary.\n\n'
        "    Yields:\n"
        "        int: each,\n"
        "            in order.\n"
        '    """\n')
    assert len(offenders) == 1 and "indented 8 spaces" in offenders[0], (
        offenders)


def test_an_empty_returns_block_is_red():
    offenders = _one("def f():\n"
                     '    """Summary.\n\n'
                     "    Returns:\n\n"
                     "    The rest.\n"
                     '    """\n')
    assert len(offenders) == 1 and "is empty" in offenders[0], offenders
    # On the opening line, `cleandoc` puts the item at the header's own
    # indent, so napoleon ends the section before it.
    offenders = _one("def f():\n"
                     '    """Returns:\n'
                     "        int: how many.\n"
                     '    """\n')
    assert len(offenders) == 1 and "is empty" in offenders[0], offenders


def test_an_untyped_returns_item_needs_a_return_annotation():
    """R2: no colon and no annotation is red; either one is enough."""
    untyped = ('    """Summary.\n\n'
               "    Returns:\n"
               "        How many.\n"
               '    """\n')
    assert _typed("def f() -> int:\n" + untyped) == []
    offenders = _typed("def f():\n" + untyped)
    assert len(offenders) == 1, offenders
    assert "names no type before a colon" in offenders[0]
    # `Type: description` needs no annotation at all.
    assert _typed("def f():\n"
                  '    """Summary.\n\n'
                  "    Returns:\n"
                  "        Optional[Dict[str, Any]]: the table, or None.\n"
                  '    """\n') == []


def test_a_sentence_before_the_colon_is_not_a_type():
    """R2's shape check: `Note: ...` would render as a type `Note`."""
    offenders = _typed("def f():\n"
                       '    """Summary.\n\n'
                       "    Returns:\n"
                       "        Note that this is prose: it is.\n"
                       '    """\n')
    assert len(offenders) == 1, offenders
    assert "is read as the returned type and is not one" in offenders[0]


def test_a_pep_604_union_before_the_colon_is_a_type():
    """R2's shape admits `|`, and only `|` (review of #637).

    `int | None: the count.` is the spelling a 3.12-floor maintainer
    writes first. `or` is not an annotation, so `np.ndarray or None:`
    stays an offender.
    """
    def returns(item):
        return _typed("def f():\n"
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
    """R3, both arms, and the star-stripped `**options` lookup."""
    doc = ('    """Summary.\n\n'
           "    Args:\n"
           "        folder: where.\n"
           "        **options: the rest.\n"
           '    """\n')
    assert _typed("def f(folder: str, **options: dict):\n" + doc) == []
    offenders = _typed("def f(folder, **options):\n" + doc)
    assert len(offenders) == 2, offenders
    assert "'folder' has no (type)" in offenders[0]
    assert "'options' has no (type)" in offenders[1]
    assert _typed("def f(folder, **options):\n"
                  '    """Summary.\n\n'
                  "    Args:\n"
                  "        folder (str): where.\n"
                  "        **options (dict): the rest.\n"
                  '    """\n') == []


def test_an_args_entry_naming_no_parameter_is_flagged():
    offenders = _typed("def f(folder: str):\n"
                       '    """Summary.\n\n'
                       "    Args:\n"
                       "        folder (str): where.\n"
                       "        fodler (str): a typo.\n"
                       '    """\n')
    assert len(offenders) == 1, offenders
    assert "names 'fodler', which the def does not take" in offenders[0]


def test_a_class_args_section_documents_its_init():
    """R3 on a class: its `Args:` is matched to `__init__`."""
    source = ("class E(RuntimeError):\n"
              '    """Summary.\n\n'
              "    Args:\n"
              "        failures (list): what failed.\n"
              "        attempted (int): how many.\n"
              "        folder (str): not a parameter.\n"
              '    """\n'
              "    def __init__(self, failures, attempted):\n"
              "        pass\n")
    offenders, graded = check_typed(source, "fixture.py")
    assert graded == 1, graded
    assert len(offenders) == 1, offenders
    assert "names 'folder', which the def does not take" in offenders[0]


def test_a_span_followed_by_a_letter_does_not_close():
    """R4, positive: `` `RuntimeError`s `` and its literal twin."""
    offenders = _one("def f():\n"
                     '    """Raise `RuntimeError`s, and ``DERIVED``s.\n'
                     '    """\n')
    assert len(offenders) == 2, offenders
    assert "'`' span does not close at '`s'" in offenders[0], offenders
    assert "'``' span does not close at '``s'" in offenders[1], offenders
    assert all("fixture.py:2:" in o for o in offenders), offenders


def test_a_span_closed_before_punctuation_or_a_suffix_is_clean():
    """R4, negative: docutils' closing punctuation, roles and wrapping."""
    assert _one("def f():\n"
                '    """Raise `RuntimeError` exceptions, `a`\'s, (`b`),\n'
                "    `c`/`d`, `e`-`f`, `g`: h; `i`! `j`? :class:`k`, `l`_,\n"
                "    ``m``. A span may wrap: `(0028,\n"
                "    0301)` and **strong\n"
                "    text** too, and x ** 2 is arithmetic.\n"
                '    """\n') == []


def test_a_span_never_closed_is_red_and_a_backslash_escapes_the_close():
    """R4: an open span, and `` `\\` ``, whose backslash escapes its close."""
    offenders = _one("def f():\n"
                     '    """Summary with a stray `here.\n\n'
                     "    Separated by `\\\\`, never closed.\n"
                     '    """\n')
    assert len(offenders) == 2, offenders
    assert "fixture.py:2:" in offenders[0] and "never closed" in offenders[0]
    assert "fixture.py:4:" in offenders[1], offenders
    assert "does not close" in offenders[1], offenders
    # Inside a span that does close, the backslash is still an escape:
    # `'RGB\\t'` renders as 'RGBt'.
    offenders = _one("def f():\n"
                     '    """Quoted as `\'RGB\\\\t\'`.\n'
                     '    """\n')
    assert len(offenders) == 1 and "read as an escape" in offenders[0], (
        offenders)
    # A double-backtick literal does no escape processing.
    assert _one("def f():\n"
                '    """Separated by ``\\\\``, as ``\'RGB\\\\t\'``.\n'
                '    """\n') == []


def test_a_span_does_not_pair_across_two_fields_or_paragraphs():
    """R4 reads each paragraph and each field as its own text block."""
    offenders = _one("def f(a, b):\n"
                     '    """Summary.\n\n'
                     "    Args:\n"
                     "        a (int): opens `here\n"
                     "        b (int): and closes` here.\n"
                     '    """\n')
    assert len(offenders) == 2, offenders
    assert "fixture.py:5:" in offenders[0], offenders
    assert "fixture.py:6:" in offenders[1], offenders


def test_a_wrapped_number_is_paragraph_text_and_list_items_are_blocks():
    """R4's blocks: `0301) NO` mid-paragraph is text; each item is its own."""
    assert _one("def f():\n"
                '    """A zone redaction writes `(0028,\n'
                "    0301) NO` beside it.\n"
                '    """\n') == []
    offenders = _one("def f():\n"
                     '    """Summary.\n\n'
                     "    - opens `here\n"
                     "    - and closes` here.\n"
                     '    """\n')
    assert len(offenders) == 2, offenders


def test_a_backtick_inside_strong_is_red():
    """R4 (S9): reST does not nest, so the backticks show."""
    offenders = _one("def f():\n"
                     '    """**What `restore=True` writes.** Then text.\n'
                     '    """\n')
    assert len(offenders) == 1, offenders
    assert "inside a '**' span" in offenders[0], offenders
    assert _one("def f():\n"
                '    """**What a restore writes.** `restore=True` then.\n'
                '    """\n') == []


def test_napoleon_escapes_a_field_names_stars_but_not_a_second_name():
    """R4 on `Args:`: `**options` leads a field; `*a, **k` holds a second."""
    assert _one("def f(**options):\n"
                '    """Summary.\n\n'
                "    Args:\n"
                "        **options (dict): the rest.\n"
                '    """\n') == []
    offenders = _one("def f(*args, **kwargs):\n"
                     '    """Summary.\n\n'
                     "    Args:\n"
                     "        *args, **kwargs: the rest.\n"
                     '    """\n')
    assert len(offenders) == 1 and "'**' is never closed" in offenders[0], (
        offenders)


def test_code_is_not_graded_for_markup():
    """R4 and R6 skip literal blocks and doctests."""
    assert _one("def f():\n"
                '    """Summary, as code::\n\n'
                "        # a comment, `x`s and **kw\n"
                "        y = `\n\n"
                "    And a doctest:\n\n"
                "    >>> # `x`s\n"
                "    >>> f(**kw)\n"
                '    """\n') == []


def test_a_markdown_fence_is_red():
    """R5: both fence lines, and nothing inside them twice."""
    offenders = _one("def f():\n"
                     '    """Summary.\n\n'
                     "    ```python\n"
                     "    # a `x`s comment\n"
                     "    ```\n"
                     '    """\n')
    assert len(offenders) == 2, offenders
    assert all("(R5)" in o for o in offenders), offenders
    assert "fixture.py:4:" in offenders[0] and "fixture.py:6:" in offenders[1]


def test_a_line_starting_with_a_hash_is_red():
    """R6: `# Heading`, and `#1108)` wrapped to a line's start."""
    offenders = _one("def f():\n"
                     '    """Summary.\n\n'
                     "    # Heading\n\n"
                     "    The policy (see\n"
                     "    #1108) holds.\n"
                     '    """\n')
    assert len(offenders) == 2, offenders
    assert all("(R6)" in o for o in offenders), offenders
    assert _one("def f():\n"
                '    """The policy (see #1108) holds.\n\n'
                "    #. an auto-numbered item.\n"
                "    #. and another.\n"
                '    """\n') == []


def test_a_markdown_link_is_red_and_a_role_or_code_is_not():
    """R7: both Markdown link shapes; the same text in code is clean."""
    offenders = _one("def f():\n"
                     '    """Returns an\n'
                     "    [ExportSummary][pkg.mod.ExportSummary], see\n"
                     "    [the docs](https://example.org/x).\n"
                     '    """\n')
    assert len(offenders) == 2, offenders
    assert all("(R7)" in o for o in offenders), offenders
    assert "fixture.py:3:" in offenders[0], offenders
    assert _one("def f():\n"
                '    """Returns an :class:`ExportSummary <pkg.mod.E>`, and\n'
                "    `grid[r][c]` and ``a[i](x)`` are code.\n"
                '    """\n') == []


def test_private_members_closures_modules_and_dunders_are_graded():
    """D3: the whole package, not what a page happens to render."""
    deep = '"""Summary.\n\nReturns:\n    int: two,\n        items.\n"""\n'
    source = (deep +
              "class C:\n"
              '    """A class."""\n'
              "    def _hidden(self):\n"
              '        """Summary.\n\n        Returns:\n            int: two,\n                items.\n        """\n'
              "    def __exit__(self, *exc):\n"
              '        """Summary.\n\n        Returns:\n            bool: two,\n                items.\n        """\n'
              "def f():\n"
              "    def inner():\n"
              '        """Summary.\n\n        Returns:\n            int: two,\n                items.\n        """\n')
    offenders, graded = check_source(source, "fixture.py")
    assert graded == 5, graded
    assert len(offenders) == 4, offenders
    for name in ("<module>", "_hidden", "__exit__", "inner"):
        assert any(f" {name}: " in o for o in offenders), (name, offenders)
    assert all("indented 8 spaces where the item sits at 4" in o
               for o in offenders), offenders


def test_the_typed_scope_is_read_from_the_api_pages_in_both_spellings(tmp_path):
    """A page's target is R2/R3's scope: a `:::` line or an autodoc line."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "mod.py").write_text(
        "class K:\n"
        '    """A class."""\n'
        "    def m(self):\n"
        '        """Summary.\n\n        Returns:\n            Some prose: here.\n        """\n'
        "    def _m(self):\n"
        '        """Summary.\n\n        Returns:\n            Some prose: here.\n        """\n'
        "def loose():\n"
        '    """Summary.\n\n    Returns:\n        Some prose: here.\n    """\n',
        encoding="utf-8")
    pages = tmp_path / "api"
    pages.mkdir()
    (pages / "k.md").write_text("# K\n\n::: pkg.mod.K\n", encoding="utf-8")

    offenders, graded, targets = check_rendered_scope(pages, pkg)

    assert targets == ["pkg.mod.K"]
    assert graded == 2, graded
    assert len(offenders) == 1 and " m: " in offenders[0], offenders

    (pages / "k.md").write_text(
        "# K\n\n```{eval-rst}\n.. autoclass:: pkg.mod.K\n   :members: m\n"
        "```\n\n```{eval-rst}\n.. autofunction:: pkg.mod.loose\n```\n\n"
        "```python\n.. autoclass:: pkg.mod.Absent\n```\n",
        encoding="utf-8")
    offenders, graded, targets = check_rendered_scope(pages, pkg)
    assert targets == ["pkg.mod.K", "pkg.mod.loose"], targets
    assert graded == 3, graded
    assert any(" loose: " in o for o in offenders), offenders

    (pages / "k.md").write_text("::: pkg.mod.K._m\n", encoding="utf-8")
    offenders, graded, _ = check_rendered_scope(pages, pkg)
    assert graded == 1 and " _m: " in offenders[0], offenders

    for absent in ("pkg.mod.absent", "pkg.mod.K.absent"):
        (pages / "k.md").write_text(f"::: {absent}\n", encoding="utf-8")
        try:
            check_rendered_scope(pages, pkg)
        except ValueError as error:
            assert "absent" in str(error)
        else:
            raise AssertionError(f"{absent} resolved")


def test_the_package_sweep_reads_every_file(tmp_path):
    """`check_package` walks subpackages and reports the file and line."""
    pkg = tmp_path / "pkg"
    (pkg / "sub").mkdir(parents=True)
    (pkg / "__init__.py").write_text('"""Clean."""\n', encoding="utf-8")
    (pkg / "sub" / "deep.py").write_text(
        "def f():\n"
        '    """Raise `RuntimeError`s."""\n', encoding="utf-8")
    offenders, graded, files = check_package(pkg)
    assert len(files) == 2 and graded == 2, (files, graded)
    assert len(offenders) == 1, offenders
    assert offenders[0].startswith("pkg/sub/deep.py:2: f: "), offenders

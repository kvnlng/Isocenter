"""The grammar for `instance_blobs.kind`.

One column, one spelling. A kind is `'pixels'`, `'waveform'`, or one of
those roots plus the path to a nested element. The column is unconstrained
`TEXT NOT NULL`, but the spelling is still a schema decision: the table's
only key is `UNIQUE(instance_uid, kind)`, and changing the spelling after
rows exist is a migration.

Kept in its own module, not in `persistence.py`: `persistence.py` imports
from `io_handlers.py`, which builds a kind at ingest, so the grammar there
would need an import cycle. Stdlib only, so it adds nothing to
`install_requires`.
"""

import re
from typing import Optional, Tuple

# --- The grammar for `instance_blobs.kind` (#183) ---
#
#   kind   := root | root ":" path
#   root   := "pixels" | "waveform"
#   path   := step ( "/" step )* "/" tag
#   step   := tag "/" index
#   tag    := [0-9a-f]{4} "," [0-9a-f]{4}
#   index  := 0 | [1-9][0-9]*
#
# In words: a blob's kind is its root, and -- when the payload sits inside a
# sequence -- a colon, then the `iter_item_tree` path to the enclosing item
# written as `tag/index` steps separated by `/`, then a final `/` and the tag
# of the element the bytes came out of.
#
# The path segment is a serialization of exactly the tuple `iter_item_tree`
# already yields (`entities.py`), and that is the point rather than a
# coincidence: this codebase already has one answer to "where in the instance
# is this", used by `PhiFinding.entity_path`, `resolve_item_path` and
# `_rehydrate_findings`. A blob path of a different shape would be a second
# answer to the same question.
#
# The terminal tag is not decoration -- it is what tells the export writeback
# which element to create. Inferring it from the root would work only until
# it did not: #277 wants (5400,1010) under a path and Q5's shape is
# (7fe0,0008) under one.
#
# **There is no escaping and none may be added.** The token alphabets are
# closed and disjoint from the delimiters: a tag is drawn from `[0-9a-f,]`
# and cannot hold `/` or `:`; an index is drawn from `[0-9]` and can hold
# neither; `:` occurs at most once. So no delimiter can appear inside a
# token, no escape is possible, and a kind that does not match is *refused*,
# never rewritten. Adding an escape would make two strings mean one key,
# under a UNIQUE index.
#
# Lowercase-only is load-bearing twice over. Tags are lowercase-hex strings
# throughout the graph, so accepting both spellings would let one payload
# occupy two rows; and because the gate refuses uppercase, no `PIXELS:...`
# row can exist, which is what makes SQLite's ASCII case-insensitive
# `LIKE 'pixels:%'` prefix read safe. A `GLOB` there would be a second guard
# against a state the gate already makes unreachable.
#
# Room to extend without a fourth spelling: no keyword is a legal `tag`
# (none contains a comma), so a later non-path qualifier -- `pixels:frame/3`,
# say -- cannot be confused with a path.
_BLOB_ROOTS = ("pixels", "waveform")
_BLOB_TAG = r"[0-9a-f]{4},[0-9a-f]{4}"
_BLOB_INDEX = r"(?:0|[1-9][0-9]*)"
_BLOB_KIND_RE = re.compile(
    r"(?:{roots})(?::(?:{tag}/{index}/)+{tag})?".format(
        roots="|".join(_BLOB_ROOTS), tag=_BLOB_TAG, index=_BLOB_INDEX))

#: What the refusal says. One string so the gate and the parser cannot
#: describe the grammar differently.
_BLOB_KIND_GRAMMAR = (
    "a blob kind is 'pixels' or 'waveform', optionally followed by ':' and "
    "the path to the element the bytes came from -- 'gggg,eeee/index' steps "
    "separated by '/', then the element's own 'gggg,eeee' tag, all "
    "lowercase hex (e.g. 'pixels:0088,0200/0/7fe0,0010'). #183's "
    "'pixels:seq:...' sketch is not the grammar: there is no 'seq' marker.")


def parse_blob_kind(kind: str) -> Tuple[str, tuple, Optional[str]]:
    """Split an `instance_blobs.kind` into its parts, or refuse it.

    Args:
        kind (str): The stored spelling, e.g. `'pixels'` or
            `'pixels:0088,0200/0/7fe0,0010'`.

    Returns:
        Tuple[str, tuple, Optional[str]]: `(root, path, terminal_tag)`.
        `path` is the tuple `iter_item_tree` yields --
        `(("0088,0200", 0), ...)` -- and is `()` for a root blob, whose
        `terminal_tag` is None. The empty tuple rather than None so that
        `for tag, index in path` is a no-op at the root and every caller can
        walk the path without first asking whether there is one.

    Raises:
        ValueError: If `kind` does not match the grammar. Refusal is the
            whole mechanism; see the grammar comment above for why no
            escaping exists.
    """
    # `fullmatch` rather than `match` with `$`: `$` also matches before a
    # trailing newline, which is exactly how a stored key acquires an
    # invisible character that no later read can match.
    if not isinstance(kind, str) or not _BLOB_KIND_RE.fullmatch(kind):
        raise ValueError(
            f"Unknown blob kind: {kind!r}. {_BLOB_KIND_GRAMMAR}")

    root, _, rest = kind.partition(":")
    if not rest:
        return root, (), None

    # The alternation is positional and unambiguous even though `0088` is
    # also a run of digits: the regex has already guaranteed an odd token
    # count >= 3 with tags at the even positions and indices at the odd
    # ones, so this walk cannot mis-assign.
    tokens = rest.split("/")
    path = tuple((tokens[i], int(tokens[i + 1]))
                 for i in range(0, len(tokens) - 1, 2))
    return root, path, tokens[-1]


def serialize_blob_kind(root: str, path: tuple,
                        terminal_tag: Optional[str]) -> str:
    """Build an `instance_blobs.kind` from its parts. The only way to.

    Nothing may assemble a kind by f-string at a call site; that puts
    several spellings of one thing in one column. This is the inverse of
    `parse_blob_kind` and re-parses its own output, so it cannot write a key
    the gate would refuse.

    Args:
        root (str): `'pixels'` or `'waveform'`.
        path (tuple): `(sequence_tag, index)` steps as `iter_item_tree`
            yields them; `()` for a root blob.
        terminal_tag (Optional[str]): The tag of the element the bytes came
            from; None for a root blob.

    Returns:
        str: The stored spelling.

    Raises:
        ValueError: If the parts do not spell a legal kind -- including a
            path with no terminal tag, or a terminal tag with no path.
    """
    if path or terminal_tag is not None:
        steps = "".join(f"{tag}/{index}/" for tag, index in path or ())
        kind = f"{root}:{steps}{terminal_tag}"
    else:
        kind = root

    # Re-parsed rather than trusted: this is the last place a malformed key
    # can be stopped before it reaches a UNIQUE index, where a bad write
    # lands as a *new row* rather than failing.
    parse_blob_kind(kind)
    return kind


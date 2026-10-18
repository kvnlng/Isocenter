"""What a `SCRUB` rule does to one text value (#1130, Clean Descriptors).

PS3.15 E.3.5's Clean Descriptors Option keeps a description and removes
the identifying information inside it. `scrub()` finds the spans of the
kinds a rule's `target` names, removes them with a connecting word
directly left of each, tidies what is left, and returns the cleaned text
and how many spans of each kind it removed. It only ever deletes
characters: the result is never longer than the value, and holds no
marker a reader could take for content.

The kinds:

- `dates` and `ids`: `text_kinds.date_spans` and `text_kinds.id_spans`,
  each in its header-text arm (`ocr=False`): no OCR confusables, and
  `ids` spares protocol tokens and decimals (#1146). Burned-in text
  keeps the other arm.
- `names`: whole-word, case-insensitive matches of the name components
  the instance itself holds in its Person Name elements (owner ruling Q2
  (a)), and, in a value exactly at its VR's maximum length, a last word
  that begins one (#1146). No model: spaCy's `PERSON` on header text
  erased clinical descriptions and missed the names (measured, #1130).
- `orgs`: `text_kinds.org_spans`, spaCy `ORG`/`FAC`/`GPE` on the
  title-cased text (owner rulings Q11 (b), R14). It needs the `nlp` extra
  and its model, and over-removes; the docs give the figures.
- `phones`, `addresses` and `sites`: `text_kinds`' regular shapes
  (#1146, owner rulings Q3-Q5 (a)).
- `initials`: no finder. A `by` and 1 to 3 capitals directly right of a
  span another kind removes in the same round goes with it (Q6 (a)).

**Changing any constant here, or the tidy rule, changes values a rule
writes.** Before 1.0.0 the behaviour digest
(`tests/support/behaviour_digest.py`) moves; after it, that is a
`CONFIG_VERSION` minor (#762).

**This module never logs, prints or warns**: its input is header text
(`tests/test_text_scrub.py` pins it by AST).
"""
import re
from typing import Dict, Iterable, List, Sequence, Tuple

from . import text_kinds
from .config_manager import VR_DUMMY

#: The kinds, in the order a refusal lists them. `config_manager` holds
#: the schema's copy (`SCRUB_KINDS`); a test keeps them equal.
SCRUB_KINDS = ("names", "dates", "ids", "orgs", "phones", "addresses",
               "sites", "initials")

#: The labels `orgs` reads; `text_kinds` holds them.
ORG_LABELS = text_kinds.ORG_LABELS

#: Words removed with a span directly right of them (owner ruling Q3 (a)):
#: `for 736-69-8140` goes whole, `Report for Breast` stays.
CONNECTORS = ("for", "at", "by", "per", "from")
#: Titles removed with a `names` span directly right of them, and with
#: no other kind's: `Dr 12345678` keeps `Dr`.
TITLES = ("dr", "mr", "mrs", "ms", "miss", "prof")

#: Fixpoint rounds: tidying can bring two fragments together, and a span
#: they form is removed in the next round.
_MAX_ROUNDS = 3

_SEPARATORS = ",;:-/|"
_STRIPPED_AT_THE_ENDS = " ,;:-/_|."


def _word_left(words: Sequence[str]):
    """A pattern for one of `words`, whole-word and in any case, with an
    optional trailing `.` and only whitespace after it, at the end of the
    text it is matched against."""
    alternation = "|".join(sorted(map(re.escape, words), key=len, reverse=True))
    return re.compile(rf"(?<![^\W_])(?:{alternation})\.?\s+\Z", re.IGNORECASE)


_CONNECTOR_LEFT = _word_left(CONNECTORS)
_TITLE_LEFT = _word_left(TITLES)
_NAME_SPLIT = re.compile(r"[\s^=\\]+")
#: A PN's five components (PS3.5 6.2): family, given, middle, prefix,
#: suffix. The first three are a person's names.
_NAME_COMPONENTS_KEPT = 3
_DUMMY_TEXTS = frozenset(value.upper() for value in VR_DUMMY.values()
                         if isinstance(value, str))


def name_tokens(person_names: Iterable) -> Tuple[str, ...]:
    """The words a `names` target removes, from Person Name values.

    Each value is split into its values (`\\`) and component groups (`=`);
    of each group's `^`-separated components the family, given and middle
    names are kept and the prefix and suffix dropped, so `Dr` or `Jr`
    never becomes a word to remove. Each component is split on
    whitespace. A token is kept when it has 2 or more characters, holds a
    letter, and is not a dummy (`ANONYMIZED`) or a pseudonym (`ANON_...`).

    Args:
        person_names: Person Name values; anything not a `str` is skipped.

    Returns:
        tuple: The tokens, upper-cased, distinct, in order of first sight.
    """
    seen = {}
    for value in person_names:
        if not isinstance(value, str):
            continue
        for one in value.split("\\"):
            for group in one.split("="):
                for component in group.split("^")[:_NAME_COMPONENTS_KEPT]:
                    for token in _NAME_SPLIT.split(component):
                        token = token.strip(".,")
                        upper = token.upper()
                        if (len(token) >= 2 and any(ch.isalpha() for ch in token)
                                and upper not in _DUMMY_TEXTS
                                and not upper.startswith("ANON_")):
                            seen.setdefault(upper, None)
    return tuple(seen)


def _name_spans(text: str, names: Sequence[str]) -> List[Tuple[int, int]]:
    if not names:
        return []
    alternation = "|".join(sorted(map(re.escape, names), key=len, reverse=True))
    pattern = re.compile(rf"(?<![^\W_])(?:{alternation})(?![^\W_])", re.IGNORECASE)
    return [m.span() for m in pattern.finditer(text)]


#: The shortest cut-off last word read as a name's beginning (#1146).
CUT_NAME_MIN_CHARACTERS = 3
#: The value's last word: letters at the very end, after whitespace or at
#: the start. A protocol segment (`..._tra`) is not a word, since `tra`,
#: `sag` and `cor` begin real family names.
_LAST_WORD = re.compile(r"(?<!\S)[^\W\d_]+\Z")


def _cut_name_span(text: str, names: Sequence[str], max_length):
    """The span of the value's last word when the value is exactly
    `max_length` long, so may have been cut at its VR's maximum, and that
    word, `CUT_NAME_MIN_CHARACTERS` or more letters, is the beginning of
    one of `names` (#1146): `... for Christopher Powe` of a patient
    `POWERS^CHRISTOPHER`. None otherwise."""
    if not names or max_length is None or len(text) != max_length:
        return None
    last = _LAST_WORD.search(text)
    if last is None or len(last.group(0)) < CUT_NAME_MIN_CHARACTERS:
        return None
    word = last.group(0).upper()
    if any(name.startswith(word) and name != word for name in names):
        return last.span()
    return None


def _spans(text: str, kinds: Sequence[str], names: Sequence[str], max_length=None):
    """`[(start, end, kind)]` for every kind in `kinds`, in `kinds` order.
    `initials` finds nothing here: it widens another kind's span
    (`_initials_right`)."""
    found = []
    for kind in kinds:
        if kind == "names":
            spans = _name_spans(text, names)
            cut = _cut_name_span(text, names, max_length)
            if cut is not None:
                spans = spans + [cut]
        elif kind == "dates":
            spans = text_kinds.date_spans(text, ocr=False)
        elif kind == "ids":
            spans = text_kinds.id_spans(text, ocr=False)
        elif kind == "initials":
            continue
        else:
            spans = text_kinds.spans(text, (kind,))[kind]
        found.extend((start, end, kind) for start, end in spans if end > start)
    return found


def _merged(found, kinds: Sequence[str], text: str):
    """Overlapping spans merged; two `names` spans with only whitespace
    between them are one name. Returns `[(start, end, kind, kinds)]`: the
    merged span, the first kind in `kinds` order that found any part of
    it (so one removal is counted once), and every kind that did."""
    order = {kind: i for i, kind in enumerate(kinds)}
    merged = []
    for start, end, kind in sorted(found):
        if merged:
            last_start, last_end, last_kinds = merged[-1]
            joins = start < last_end or (
                kind == "names" and "names" in last_kinds
                and not text[last_end:start].strip())
            if joins:
                merged[-1] = (last_start, max(last_end, end), last_kinds | {kind})
                continue
        merged.append((start, end, {kind}))
    return [(start, end, min(here, key=order.__getitem__), here)
            for start, end, here in merged]


def _widened(text: str, start: int, kinds_found) -> int:
    """`start` moved left over a title (before a name) and a connector
    directly left of the span."""
    if "names" in kinds_found:
        title = _TITLE_LEFT.search(text[:start])
        if title is not None:
            start = title.start()
    connector = _CONNECTOR_LEFT.search(text[:start])
    if connector is not None:
        start = connector.start()
    return start


#: Staff initials directly right of a removed span (owner ruling Q6 (a)):
#: whitespace, a lower-case `by`, then 1 to 3 capitals as a whole word,
#: `_` included in the word (`by SS_2` is not taken).
_INITIALS_RIGHT = re.compile(r"\s+by\s+[A-Z]{1,3}(?!\w)")


def _initials_right(text: str, end: int) -> int:
    """`end` moved right over `by SS` directly after it, or `end`."""
    initials = _INITIALS_RIGHT.match(text, end)
    return initials.end() if initials is not None else end


_LINE_BREAK = re.compile(r"\r\n|\n|\r")


def _joint(gap: str) -> str:
    """What the whitespace at a cut becomes: nothing when there was none,
    its first line break when it held one, otherwise one space."""
    if not gap:
        return ""
    line_break = _LINE_BREAK.search(gap)
    return line_break.group(0) if line_break is not None else " "


def _cut(text: str, spans) -> str:
    """`text` without `spans`, tidied at each cut and nowhere else (#1146).

    At a cut, the whitespace either side of it (and after a dropped
    separator) becomes `_joint`'s: nothing, a line break or one space;
    none at all when the cut is at either end of what is left, or when
    `,`, `;` or `:` follows. Of two `,;:-/|` meeting at a cut, one
    survives. An end of the value a cut reached is stripped of
    whitespace and `,;:-/_|.`. Whitespace away from a cut is the text's
    own.
    """
    out = ""
    position = 0
    for start, end in spans:
        left = out + text[position:start]
        kept_left = left.rstrip()
        rest = text[end:]
        kept_rest = rest.lstrip()
        gap = left[len(kept_left):] + rest[:len(rest) - len(kept_rest)]
        if (kept_left and kept_rest and kept_left[-1] in _SEPARATORS
                and kept_rest[0] in _SEPARATORS):
            # `A - 1234567 - B`: one separator survives the cut, not two.
            after = kept_rest[1:]
            gap += after[:len(after) - len(after.lstrip())]
            kept_rest = after.lstrip()
        position = len(text) - len(kept_rest)
        if not kept_left or not kept_rest or kept_rest[0] in ",;:":
            # `MR BREAS, UNIT` cut to `MR, UNIT`, not `MR , UNIT`.
            out = kept_left
        else:
            out = kept_left + _joint(gap)
    out += text[position:]
    # An end is tidied only where a cut reached it: what was before the
    # first cut, or after the last, is nothing but those characters.
    if not text[:spans[0][0]].strip(_STRIPPED_AT_THE_ENDS):
        out = out.lstrip(_STRIPPED_AT_THE_ENDS)
    if not text[spans[-1][1]:].strip(_STRIPPED_AT_THE_ENDS):
        out = out.rstrip(_STRIPPED_AT_THE_ENDS)
    return out


def _scrub_once(text: str, kinds: Sequence[str], names: Sequence[str],
                max_length=None):
    found = _spans(text, kinds, names, max_length)
    if not found:
        return text, {}
    merged = _merged(found, kinds, text)
    widened = []
    counts: Dict[str, int] = {}
    for start, end, kind, here in merged:
        counts[kind] = counts.get(kind, 0) + 1
        start = _widened(text, start, here)
        if "initials" in kinds:
            # In this round, beside the span it follows: the next round
            # would see `Ordered by SS` with nothing removed before it.
            right = _initials_right(text, end)
            if right != end:
                counts["initials"] = counts.get("initials", 0) + 1
                end = right
        if widened and start <= widened[-1][1]:
            widened[-1] = (widened[-1][0], max(end, widened[-1][1]))
            continue
        widened.append((start, end))
    return _cut(text, widened), counts


def scrub(text: str, kinds: Sequence[str], names: Sequence[str] = (),
          max_length=None) -> Tuple[str, Dict[str, int]]:
    """`text` with the spans of `kinds` removed, and the count per kind.

    Each kind's spans are found, overlapping spans merged (a span two
    kinds found counts once, for the first of them in `kinds`), each
    widened left over a title (`names` only) and a connecting word
    directly before it, and, under `initials`, right over a `by` and 1
    to 3 capitals directly after it; then cut out. The tidy is at each
    cut only (#1146): the whitespace there becomes one space, or the
    line break it held, or nothing before `,`, `;` or `:`; one of two
    separators meeting there is dropped; and an end of the value a cut
    reached is stripped of whitespace and `,;:-/_|.`. Whitespace away from a cut is
    kept as written. Brackets are not balanced. Repeated until nothing
    more is found, at most three rounds, so `scrub` of its own result
    changes nothing. A value nothing is found in is returned as it is.

    Args:
        text (str): The value.
        kinds (Sequence[str]): The rule's `target`, in its order.
        names (Sequence[str]): The `names` kind's words (`name_tokens`).
        max_length (int, optional): The element's VR's maximum length in
            characters (`config_manager._text_max_characters`). A value
            exactly that long may have been cut there, so under `names`
            its last word is a name when it begins one (#1146). Read in
            the first round only: only the value as read can have been
            cut.

    Returns:
        tuple: `(cleaned, {kind: count})`; `cleaned` may be `""`, which the
        scan writes as the VR's dummy.

    Raises:
        Exception: What `text_kinds.org_spans` raises when `orgs` is asked
            for and the model cannot load.
    """
    counts: Dict[str, int] = {}
    current = text
    for round_ in range(_MAX_ROUNDS):
        cleaned, found = _scrub_once(current, kinds, names,
                                     max_length if round_ == 0 else None)
        for kind, number in found.items():
            counts[kind] = counts.get(kind, 0) + number
        if cleaned == current:
            break
        current = cleaned
    return current, counts

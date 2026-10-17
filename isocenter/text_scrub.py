"""What a `SCRUB` rule does to one text value (#1130, Clean Descriptors).

PS3.15 E.3.5's Clean Descriptors Option keeps a description and removes
the identifying information inside it. `scrub()` finds the spans of the
kinds a rule's `target` names, removes them with a connecting word
directly left of each, tidies what is left, and returns the cleaned text
and how many spans of each kind it removed. It only ever deletes
characters: the result is never longer than the value, and holds no
marker a reader could take for content.

The kinds:

- `dates` and `ids`: `text_kinds.date_spans` (with no OCR confusables)
  and `text_kinds.id_spans`, the regular expressions burned-in text uses.
- `names`: whole-word, case-insensitive matches of the name components
  the instance itself holds in its Person Name elements (owner ruling Q2
  (a)). No model: spaCy's `PERSON` on header text erased clinical
  descriptions and missed the names (measured, #1130).
- `orgs`: `text_kinds.org_spans`, spaCy `ORG`/`FAC`/`GPE` on the
  title-cased text (owner rulings Q11 (b), R14). It needs the `nlp` extra
  and its model, and over-removes; the docs give the figures.

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
SCRUB_KINDS = ("names", "dates", "ids", "orgs")

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
_SPACE_RUN = re.compile(r"\s+")
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


def _spans(text: str, kinds: Sequence[str], names: Sequence[str]):
    """`[(start, end, kind)]` for every kind in `kinds`, in `kinds` order."""
    found = []
    for kind in kinds:
        if kind == "names":
            spans = _name_spans(text, names)
        elif kind == "dates":
            spans = text_kinds.date_spans(text, ocr=False)
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


def _cut(text: str, spans) -> str:
    """`text` without `spans`, tidied at each cut and at the ends."""
    out = ""
    position = 0
    for start, end in spans:
        out += text[position:start]
        position = end
        rest = text[position:].lstrip()
        left = out.rstrip()
        if left and rest and left[-1] in _SEPARATORS and rest[0] in _SEPARATORS:
            # `A - 1234567 - B`: one separator survives the cut, not two.
            position = len(text) - len(rest) + 1
        elif rest[:1] in (",", ";", ":"):
            # `MR BREAS, UNIT` cut to `MR, UNIT`, not `MR , UNIT`.
            out = left
    out += text[position:]
    return _SPACE_RUN.sub(" ", out).strip(_STRIPPED_AT_THE_ENDS)


def _scrub_once(text: str, kinds: Sequence[str], names: Sequence[str]):
    found = _spans(text, kinds, names)
    if not found:
        return text, {}
    merged = _merged(found, kinds, text)
    widened = []
    counts: Dict[str, int] = {}
    for start, end, kind, here in merged:
        counts[kind] = counts.get(kind, 0) + 1
        start = _widened(text, start, here)
        if widened and start <= widened[-1][1]:
            widened[-1] = (widened[-1][0], max(end, widened[-1][1]))
            continue
        widened.append((start, end))
    return _cut(text, widened), counts


def scrub(text: str, kinds: Sequence[str], names: Sequence[str] = ()
          ) -> Tuple[str, Dict[str, int]]:
    """`text` with the spans of `kinds` removed, and the count per kind.

    Each kind's spans are found, overlapping spans merged (a span two
    kinds found counts once, for the first of them in `kinds`), each
    widened left over a title (`names` only) and a connecting word
    directly before it, and cut out. At each cut one of two separators
    meeting there is dropped, and whitespace left before `,`, `;` or `:`;
    whitespace runs collapse to one space, and the ends are stripped of
    whitespace and ` ,;:-/_|.`. Brackets are not balanced. Repeated until
    nothing more is found, at most three rounds, so `scrub` of its own
    result changes nothing. A value nothing is found in is returned as it
    is, untidied.

    Args:
        text (str): The value.
        kinds (Sequence[str]): The rule's `target`, in its order.
        names (Sequence[str]): The `names` kind's words (`name_tokens`).

    Returns:
        tuple: `(cleaned, {kind: count})`; `cleaned` may be `""`, which the
        scan writes as the VR's dummy.

    Raises:
        Exception: What `text_kinds.org_spans` raises when `orgs` is asked
            for and the model cannot load.
    """
    counts: Dict[str, int] = {}
    current = text
    for _ in range(_MAX_ROUNDS):
        cleaned, found = _scrub_once(current, kinds, names)
        for kind, number in found.items():
            counts[kind] = counts.get(kind, 0) + number
        if cleaned == current:
            break
        current = cleaned
    return current, counts

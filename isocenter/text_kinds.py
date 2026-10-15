"""Kinds of text a narrowed `burned_in_text` target names (#1121).

Pure functions over one line of text OCR read, each returning the
`(start, end)` character spans that look like its kind: `dates` (date
shapes), `ids` (long runs of digits) and `names` (spaCy `PERSON`
entities). Nothing here compares a reading with any patient's values
(owner ruling of 2026-10-15: a burned-in name may be misspelled, and the
source is not read again), and nothing here keeps or returns text: only
spans, which never leave the `burned_in_text` worker.

**This module never logs, prints or warns**, and lets no warning of the
model's out: its inputs are read text, and a log line at any level is a
leak (`tests/test_text_kinds.py` pins it by AST).
"""
import re
import threading
import warnings
from typing import Dict, Iterable, List, Optional, Tuple

Span = Tuple[int, int]

#: The kinds, in the order a refusal lists them. `config_manager` holds
#: the schema's copy (`BURNED_IN_TEXT_KINDS`); a test keeps them equal.
KINDS = ("names", "dates", "ids")

# --- dates -------------------------------------------------------------------

#: OCR's digit confusables, mapped on a copy inside a candidate span only,
#: so `0I/O2/197O` is a date and a word elsewhere on the line is untouched.
_CONFUSABLES = str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1",
                              "|": "1", "S": "5", "B": "8"})
_DIGITISH = "0-9OoIl|SB"
#: Three numeric fields with one separator, used twice (`\2`). The
#: lookarounds keep a field from being the tail of a longer run.
_NUMERIC_DATE = re.compile(
    rf"(?<![0-9A-Za-z])([{_DIGITISH}]{{1,4}})([/.\- ])([{_DIGITISH}]{{1,2}})"
    rf"\2([{_DIGITISH}]{{1,4}})(?![0-9A-Za-z])")
#: Eight digits read as the DICOM DA form, `YYYYMMDD`.
_COMPACT_DATE = re.compile(rf"(?<![0-9A-Za-z])[{_DIGITISH}]{{8}}(?![0-9A-Za-z])")
_MONTHS = {name: number for number, names in enumerate((
    ("jan", "january"), ("feb", "february"), ("mar", "march"),
    ("apr", "april"), ("may",), ("jun", "june"), ("jul", "july"),
    ("aug", "august"), ("sep", "sept", "september"), ("oct", "october"),
    ("nov", "november"), ("dec", "december")), 1) for name in names}
_MONTH_WORD = re.compile(
    r"(?<![A-Za-z0-9])(" + "|".join(sorted(_MONTHS, key=len, reverse=True))
    + r")\.?(?![A-Za-z0-9])", re.IGNORECASE)
#: What may sit between a month word and its day or year.
_GAP = r"[\s,./\-]{0,3}"
_NUMBER_BEFORE = re.compile(r"(?<![0-9A-Za-z])(\d{1,2})" + _GAP + r"$")
_NUMBER_AFTER = re.compile(r"^" + _GAP + r"(\d{1,4})(?![0-9A-Za-z])")
_YEAR_AFTER = re.compile(r"^" + r"[\s,./\-]{1,3}" + r"(\d{2}|\d{4})(?![0-9A-Za-z])")


def _year_ok(text: str) -> bool:
    """A 4-digit year from 1800 to 2199, or any 2-digit year."""
    return len(text) == 2 or (len(text) == 4 and 1800 <= int(text) <= 2199)


def _day_ok(text: str) -> bool:
    return len(text) <= 2 and 1 <= int(text) <= 31


def _month_ok(text: str) -> bool:
    return len(text) <= 2 and 1 <= int(text) <= 12


def _numeric_date_ok(first: str, second: str, third: str) -> bool:
    """`Y-M-D`, `D-M-Y` or `M-D-Y`, judged by field ranges."""
    if len(first) == 4:
        return _year_ok(first) and _month_ok(second) and _day_ok(third)
    if len(first) > 2 or len(third) not in (2, 4) or not _year_ok(third):
        return False
    return ((_day_ok(first) and _month_ok(second))
            or (_month_ok(first) and _day_ok(second)))


def _holds_a_digit(text: str) -> bool:
    return any(ch.isdigit() for ch in text)


def _numeric_dates(line: str) -> List[Span]:
    found = []
    for match in _NUMERIC_DATE.finditer(line):
        if not _holds_a_digit(match.group(0)):
            continue
        fields = [match.group(i).translate(_CONFUSABLES) for i in (1, 3, 4)]
        if all(f.isdigit() for f in fields) and _numeric_date_ok(*fields):
            found.append(match.span())
    for match in _COMPACT_DATE.finditer(line):
        if not _holds_a_digit(match.group(0)):
            continue
        text = match.group(0).translate(_CONFUSABLES)
        if (text.isdigit() and _year_ok(text[:4]) and _month_ok(text[4:6])
                and _day_ok(text[6:])):
            found.append(match.span())
    return found


def _month_dates(line: str) -> List[Span]:
    """A month name beside a day and/or a year: `01 JAN 1970`,
    `Jan 1, 1970`, `JAN-1970`, `January 5`, `5 Sept`. A month word with no
    number beside it is not a date (`MAY BE`)."""
    found = []
    for match in _MONTH_WORD.finditer(line):
        start, end = match.span()
        before = _NUMBER_BEFORE.search(line[:start])
        day_before = before is not None and _day_ok(before.group(1))
        after = _NUMBER_AFTER.match(line[end:])
        tail = None
        if after is not None:
            number = after.group(1)
            if len(number) == 4 and _year_ok(number):
                tail = end + after.end()
            elif _day_ok(number):
                tail = end + after.end()
                # `Jan 1, 1970`: a year after the day.
                year = _YEAR_AFTER.match(line[tail:])
                if year is not None and _year_ok(year.group(1)):
                    tail += year.end()
            elif day_before and len(number) == 2:
                # `01 JAN 70`: a 2-digit year only after a day.
                tail = end + after.end()
        if not day_before and tail is None:
            continue
        found.append((before.start(1) if day_before else start,
                      tail if tail is not None else end))
    return found


def date_spans(line: str) -> List[Span]:
    """Spans of `line` that look like a date.

    Numeric `D-M-Y`, `M-D-Y` and `Y-M-D` with one separator (`/`, `.`,
    `-` or a space) used twice and a 2- or 4-digit year (1800-2199);
    `YYYYMMDD`; and an English month name, full or three-letter, in any
    case, beside a day and/or a year. A day/month pair must read as day
    1-31 and month 1-12 in at least one order. Times of day are not dates
    (owner ruling Q-B13 (a)): a colon is no separator.
    """
    return sorted(set(_numeric_dates(line) + _month_dates(line)))


# --- ids ---------------------------------------------------------------------

#: A maximal run of ASCII letters and digits, `-` or `_` allowed inside.
_RUN = re.compile(r"[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*")
#: Owner ruling Q-B14 (a): spares `120kV`, `5.0MHz`, `1024`, `10cm`,
#: `L5-S1`; takes medical record and accession numbers.
ID_MIN_CHARACTERS = 6
ID_MIN_DIGITS = 5


def id_spans(line: str) -> List[Span]:
    """Spans of `line` that look like an identifier: a run of at least
    `ID_MIN_CHARACTERS` characters (inner `-`/`_` counted) holding at
    least `ID_MIN_DIGITS` digits."""
    return [m.span() for m in _RUN.finditer(line)
            if len(m.group(0)) >= ID_MIN_CHARACTERS
            and sum(ch.isdigit() for ch in m.group(0)) >= ID_MIN_DIGITS]


# --- names -------------------------------------------------------------------

#: The one model per process, once loaded; a failure is not cached, so
#: the next call tries again.
_MODEL = None
#: Every call into the model, and its load, is under this lock. A leaf:
#: this module takes no other lock of its own and never logs, so it cannot
#: meet `entities.PIXEL_STATE_LOCK` in either order. One `Language` is
#: shared by the threads of a pass; tesseract dominates the time, so
#: serialising the model costs little.
_NLP_LOCK = threading.Lock()
#: A `^` between letters: a DICOM Person Name shape, found beside spaCy's
#: entities, never instead of them.
_CARET_NAME = re.compile(r"[^\W\d_]+(?:\^+[^\W\d_]+)+")


def _load_model():
    """Import spaCy and load `en_core_web_sm`. The seam tests replace."""
    import spacy  # pylint: disable=import-outside-toplevel
    return spacy.load("en_core_web_sm")


def _spacy_version() -> str:
    import spacy  # pylint: disable=import-outside-toplevel
    return str(spacy.__version__)


def _loaded_model():
    """The model, loading it if needed; raises what the load raises."""
    global _MODEL  # pylint: disable=global-statement
    with _NLP_LOCK:
        if _MODEL is None:
            # Under the lock so two threads do not load twice. The load
            # imports, which takes the import lock: nothing that holds it
            # waits on this lock, so the order cannot invert.
            _MODEL = _load_model()
        return _MODEL


def _nlp():
    """The process's spaCy model. Raises when it cannot be loaded."""
    return _loaded_model()


def _nlp_unavailable_reason() -> Optional[str]:
    """None when the `names` kind can run here, otherwise why not.

    Loads the model (caching success only). The reason is the load's
    failure, which concerns loading and never holds read text.
    """
    try:
        _nlp()
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return _described(exc)
    return None


def _described(exc: BaseException) -> str:
    """`Type: message`, as `logger.describe_exception` spells it, here so
    this module imports nothing that logs."""
    try:
        message = str(exc).strip()
    except Exception:  # pylint: disable=broad-exception-caught
        message = ""
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _names_by() -> str:
    """The identity of what finds names: spaCy's and the model's versions.

    Held in each record read under `names`, so `redact()` reads again an
    image a different model read (owner ruling of 2026-10-15). Not PHI.
    """
    return (f"spacy {_spacy_version()} en_core_web_sm "
            f"{_nlp().meta['version']}")


def _title_cased(line: str) -> str:
    """`line` title-cased, one character for one, so a span found on it is
    a span of `line`: a mapping that changes length (`ß` to `SS`) keeps
    the character as read."""
    out = []
    after_letter = False
    for ch in line:
        mapped = ch.lower() if after_letter else ch.upper()
        out.append(mapped if len(mapped) == 1 else ch)
        after_letter = ch.isalpha()
    return "".join(out)


def _person_spans(model, text: str) -> List[Span]:
    return [(ent.start_char, ent.end_char) for ent in model(text).ents
            if ent.label_ == "PERSON"]


def name_spans(line: str) -> List[Span]:
    """Spans of `line` that spaCy reads as a `PERSON` (never `ORG`, owner
    ruling Q-B12 (a)), on the line as read and title-cased, since
    `en_core_web_sm` misses most upper-case names; plus each `^` between
    letters. There is no fallback: with no model this raises (Q-B11 (b)).
    """
    model = _nlp()
    found = {m.span() for m in _CARET_NAME.finditer(line)}
    with _NLP_LOCK, warnings.catch_warnings():
        # Nothing promises a model warning never quotes its input.
        warnings.simplefilter("ignore")
        found.update(_person_spans(model, line))
        titled = _title_cased(line)
        if titled != line:
            found.update(_person_spans(model, titled))
    return sorted(found)


_FINDERS = {"names": name_spans, "dates": date_spans, "ids": id_spans}


def spans(line: str, kinds: Iterable[str]) -> Dict[str, List[Span]]:
    """Each asked kind's spans on `line`; a kind not asked is not run."""
    return {kind: _FINDERS[kind](line) for kind in kinds}

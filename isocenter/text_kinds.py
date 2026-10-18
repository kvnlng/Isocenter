"""Kinds of text a narrowed `burned_in_text` target names (#1121), and
the kinds a `SCRUB` rule removes from header text (#1130).

Pure functions over one line of text, each returning the `(start, end)`
character spans that look like its kind: `dates` (date shapes), `ids`
(long runs of digits), `names` (spaCy `PERSON` entities, burned-in text
only), `orgs` (spaCy `ORG`, `FAC` and `GPE` entities, header text
only), and `phones`, `addresses` and `sites` (regular shapes, header
text only, #1146). Nothing here compares a reading with any patient's values
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
#: the schema's copies: `BURNED_IN_TEXT_KINDS`, `names`, `dates` and `ids`
#: (owner ruling Q-B12 stands for pixels), and `SCRUB_KINDS`, every kind
#: here and `initials`, which is a widening in `text_scrub`, not a finder
#: (#1130, #1146). A test keeps each a subset of this.
KINDS = ("names", "dates", "ids", "orgs", "phones", "addresses", "sites")

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
#: The same two shapes over digits only, for text no OCR read (header
#: text, #1130): `SO 2O` there is words, not a misread `50 20`.
_NUMERIC_DATE_DIGITS = re.compile(
    r"(?<![0-9A-Za-z])([0-9]{1,4})([/.\- ])([0-9]{1,2})"
    r"\2([0-9]{1,4})(?![0-9A-Za-z])")
_COMPACT_DATE_DIGITS = re.compile(r"(?<![0-9A-Za-z])[0-9]{8}(?![0-9A-Za-z])")
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


def _numeric_dates(line: str, ocr: bool = True) -> List[Span]:
    found = []
    numeric, compact = ((_NUMERIC_DATE, _COMPACT_DATE) if ocr
                        else (_NUMERIC_DATE_DIGITS, _COMPACT_DATE_DIGITS))
    for match in numeric.finditer(line):
        if not _holds_a_digit(match.group(0)):
            continue
        fields = [match.group(i).translate(_CONFUSABLES) for i in (1, 3, 4)]
        if all(f.isdigit() for f in fields) and _numeric_date_ok(*fields):
            found.append(match.span())
    for match in compact.finditer(line):
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


def date_spans(line: str, ocr: bool = True) -> List[Span]:
    """Spans of `line` that look like a date.

    Numeric `D-M-Y`, `M-D-Y` and `Y-M-D` with one separator (`/`, `.`,
    `-` or a space) used twice and a 2- or 4-digit year (1800-2199);
    `YYYYMMDD`; and an English month name, full or three-letter, in any
    case, beside a day and/or a year. A day/month pair must read as day
    1-31 and month 1-12 in at least one order. Times of day are not dates
    (owner ruling Q-B13 (a)): a colon is no separator.

    With `ocr` (the default, for burned-in text) a numeric field may hold
    OCR's digit confusables (`O`, `I`, `l`, `|`, `S`, `B`); without it
    (header text, #1130) only digits. A dotted triple whose last field
    has two digits reads as a date (`2.1.10`), by owner ruling Q8 (a).
    """
    return sorted(set(_numeric_dates(line, ocr) + _month_dates(line)))


# --- ids ---------------------------------------------------------------------

#: A maximal run of ASCII letters and digits, `-` or `_` allowed inside.
_RUN = re.compile(r"[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*")
#: Owner ruling Q-B14 (a): spares `120kV`, `5.0MHz`, `1024`, `10cm`,
#: `L5-S1`; takes medical record and accession numbers.
ID_MIN_CHARACTERS = 6
ID_MIN_DIGITS = 5


#: SCRUB's run (#1146): `^` joins too, so `P4^P114^S301^I00004` is one
#: protocol token, not four runs of which the last looks like an id.
_HEADER_RUN = re.compile(r"[A-Za-z0-9]+(?:[-_^][A-Za-z0-9]+)*")
#: Inside a protocol token only an SSN shape or this many consecutive
#: digits is an identifier (#1146).
ID_MIN_RUN_IN_TOKEN = 7
_ID_IN_TOKEN = re.compile(
    r"(?<![0-9])(?:[0-9]{3}-[0-9]{2}-[0-9]{4}|[0-9]{%d,})(?![0-9])"
    % ID_MIN_RUN_IN_TOKEN)


def _is_an_id(run: str) -> bool:
    return (len(run) >= ID_MIN_CHARACTERS
            and sum(ch.isdigit() for ch in run) >= ID_MIN_DIGITS)


def _in_a_decimal(line: str, start: int, end: int) -> bool:
    """The run `line[start:end]` is joined by a `.` to a digit on either
    side: part of a decimal number (`0.781250`, `739204.5`)."""
    return ((start >= 2 and line[start - 1] == "." and line[start - 2].isdigit())
            or (end + 1 < len(line) and line[end] == "." and line[end + 1].isdigit()))


def id_spans(line: str, ocr: bool = True) -> List[Span]:
    """Spans of `line` that look like an identifier: a run of at least
    `ID_MIN_CHARACTERS` characters (inner `-`/`_` counted) holding at
    least `ID_MIN_DIGITS` digits.

    With `ocr` (the default, for burned-in text) that is the whole rule.
    Without it (SCRUB's header text, #1146) `^` joins a run too, and two
    shapes are spared: a run joined by `.` to a digit on either side (a
    decimal's part, so `0.781250` keeps `781250`), and a protocol token,
    a run joined by `_` or `^` that holds a letter (`t2_tse_tra_320_p2`),
    which is an identifier only when it holds an SSN shape
    (`ddd-dd-dddd`) or `ID_MIN_RUN_IN_TOKEN` consecutive digits; then the
    whole token is the span. Burned-in `ids` keeps the first rule (owner
    ruling Q7 (a)), so no record is read again.
    """
    if ocr:
        return [m.span() for m in _RUN.finditer(line) if _is_an_id(m.group(0))]
    found = []
    for match in _HEADER_RUN.finditer(line):
        start, end = match.span()
        run = match.group(0)
        if _in_a_decimal(line, start, end):
            continue
        if (("_" in run or "^" in run) and any(ch.isalpha() for ch in run)
                and _ID_IN_TOKEN.search(run) is None):
            continue
        if _is_an_id(run):
            found.append((start, end))
    return found


# --- phones, addresses, sites (header text only, #1146) -----------------------

#: North American numbers with `-` or `.` between the groups, or a
#: bracketed area code, an optional leading `1`/`+1`, and an optional
#: extension (owner ruling Q3 (a)). No space between the groups, so
#: `Matrix 512 256 1024` is not a phone.
_NANP = re.compile(
    r"(?<![\w+])(?:\+?1[-.\s])?(?:\(\d{3}\)\s?|\d{3}[-.])\d{3}[-.]\d{4}"
    r"(?:\s?(?:x|ext\.?|extension)\s?\d{1,6})?(?!\w)", re.IGNORECASE)
#: E.164: `+`, then 8 to 15 digits, separators allowed after the `+`.
_E164 = re.compile(r"(?<![\w+])\+[1-9](?:[\s.-]?\d){7,14}(?!\w)")


def phone_spans(line: str) -> List[Span]:
    """Spans of `line` that look like a telephone number (#1146): a North
    American number (`(312)555-0147x12`, `312-555-0183`,
    `312.555.0129 ext 204`) or an international one with its `+`
    (`+44 20 7946 0958`). A number triple written like a phone number
    (`120-240-3600`) is taken too."""
    return sorted({m.span() for m in _NANP.finditer(line)}
                  | {m.span() for m in _E164.finditer(line)})


#: Street suffixes, case-sensitive (`Ct` is a suffix, `CT` a modality).
_STREET_SUFFIX = (r"(?:St|Ave|Av|Dr|Ct|Trl|Ln|Rd|Blvd|Way|Pl|Ter|Cir|Pkwy|Hwy|Sq|"
                  r"Street|Avenue|Drive|Court|Trail|Lane|Road|Boulevard|Place|"
                  r"Terrace|Circle|Parkway|Highway)")
_ADDRESS = re.compile(
    r"(?<![\w])\d{1,6}\s+(?:[A-Z][a-z]+(?:-[A-Z][a-z]+)?\s+){1,3}"
    + _STREET_SUFFIX
    + r"\.?,?\s+(?:[A-Z][a-z]+\s){0,2}[A-Z][a-z]+,\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?(?!\w)")


def address_spans(line: str) -> List[Span]:
    """Spans of `line` that look like a full street address (#1146, owner
    ruling Q4 (a)): a street number, one to three title-case words, a
    street suffix, then a city, a two-letter state and a ZIP
    (`157 Chisholm Trl Thibodaux, LA 70301`). A street line alone is not
    taken: `4 Phase Abdomen Ct` would be one too."""
    return [m.span() for m in _ADDRESS.finditer(line)]


#: A site acronym: 4 to 8 capitals as a whole word directly after a
#: lower-case `at` (owner ruling Q5 (a)).
_SITE = re.compile(r"(?<![^\W_])at\s+([A-Z]{4,8})(?![^\W_])")


def site_spans(line: str) -> List[Span]:
    """Spans of the acronym in `at CHAJH` (#1146): 4 to 8 capitals after a
    lower-case `at`. `at` itself is a connecting word, which SCRUB
    removes with the span. A 3-letter site (`at FGH`) is missed, so that
    `at RUL` and `at MRI` are kept."""
    return [m.span(1) for m in _SITE.finditer(line)]


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


#: The model's warnings this module ignores: W030 and W102 only, the two
#: of `spacy.errors.Warnings` whose text quotes the text read (a line of
#: burned-in text is identifying, and nothing read may reach a log). A
#: library does not silence a dependency's diagnostics for its host: the
#: 0.9.x ruling that took `filterwarnings("ignore", module="pydicom.*")`
#: out of `isocenter/__init__.py` (CHANGELOG, "importing Isocenter no
#: longer silences pydicom's warnings"). So every other spaCy and thinc
#: warning reaches the host, by owner ruling of 2026-10-15. Not covered:
#: a text-quoting warning a later spaCy adds under another code. The
#: record's `names_by` ties each read to the spaCy and model versions it
#: was made with, so such a change is a known version, not a silent one.
#: The module arm holds spaCy, thinc, and this module, which a
#: `stacklevel` walking out of spaCy can name.
_MODEL_WARNING_TEXT = r"\[W030\]|\[W102\]"
_MODEL_WARNINGS = r"(spacy|thinc|isocenter\.text_kinds)(\..*)?\Z"
#: The entry `_ignore_model_warnings`' `filterwarnings` puts in the filter
#: list: the message compiled case-insensitively, as `filterwarnings`
#: compiles it (compiled patterns compare by pattern and flags).
_MODEL_WARNINGS_FILTER = ("ignore", re.compile(_MODEL_WARNING_TEXT, re.I), Warning,
                          re.compile(_MODEL_WARNINGS), 0)


def _filters_in_force() -> list:
    """The warning filter list `filterwarnings` writes to here and now.

    On 3.12, and on 3.14 with the GIL, that is `warnings.filters`. On the
    free-threaded 3.14 build warnings are context-aware by default
    (`sys.flags.context_aware_warnings`): inside a `catch_warnings()`
    block `filterwarnings` writes to the block's own list, which
    `warnings.filters` does not show, so a presence check reading
    `warnings.filters` there would insert again on every call or miss a
    filter that is gone. `warnings._get_filters` is that interpreter's
    own reader; absent before 3.14, where `warnings.filters` is the one
    list. Measured on 3.14.7t.
    """
    get = getattr(warnings, "_get_filters", None)
    return get() if get is not None else warnings.filters


def _ignore_model_warnings() -> None:
    """Make sure one `ignore` filter for the model's two text-quoting
    warnings is in force (#1121; `_MODEL_WARNING_TEXT` says which and
    why only those).

    An insert, never `warnings.catch_warnings()` around each call: on
    3.12 that context manager saves and restores the process-global
    filter list, so on a threaded path it drops sibling threads' warnings
    while it is open and can undo another thread's filters, its own
    included, when it closes. The repo forbids it on threaded paths for
    that reason (`imagecodecs_handler.py`, above the excess-fragment
    walk; `io_handlers.py`, the IS overflow read and the
    encapsulated-length window).

    Checked on every use of the model, not only at its load: a filter
    inserted inside someone else's `catch_warnings()` block (a caller's,
    or pytest's around each test) goes with the block, and the model,
    loaded there and cached, warned again afterwards (review of #1124 at
    bc02c116). Inserted only when missing, so a caller's own filters keep
    their order otherwise. Once in force it stays for the process, so
    W030 and W102 are ignored in `discover_redaction_zones()` and the
    caller's own spaCy use too; every other warning reaches the host.
    """
    if _MODEL_WARNINGS_FILTER not in _filters_in_force():
        warnings.filterwarnings("ignore", message=_MODEL_WARNING_TEXT,
                                module=_MODEL_WARNINGS)


def _loaded_model():
    """The model, loading it if needed; raises what the load raises."""
    global _MODEL  # pylint: disable=global-statement
    with _NLP_LOCK:
        # The filter first, so the load's own warnings are ignored too.
        _ignore_model_warnings()
        if _MODEL is None:
            # Under the lock so two threads do not load twice. The load
            # imports, which takes the import lock: nothing that holds it
            # waits on this lock, so the order cannot invert.
            _MODEL = _load_model()
        return _MODEL


def _nlp():
    """The process's spaCy model. Raises when it cannot be loaded."""
    return _loaded_model()


def _nlp_load_failure() -> Optional[BaseException]:
    """None when the `names` kind can run here, otherwise the load's
    exception. Loads the model (caching success only)."""
    try:
        _nlp()
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return exc
    return None


def _nlp_unavailable_reason() -> Optional[str]:
    """None when the `names` kind can run here, otherwise why not, for
    the caller's exception message. The reason is the load's failure,
    which concerns loading and never holds read text; an audit row
    quotes its class only (`pixel_analysis._text_boxes`)."""
    exc = _nlp_load_failure()
    return None if exc is None else _described(exc)


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
    # The model's two text-quoting warnings are ignored by the filter
    # `_nlp()` keeps in force (`_ignore_model_warnings`), not by a
    # context manager here.
    with _NLP_LOCK:
        found.update(_person_spans(model, line))
        titled = _title_cased(line)
        if titled != line:
            found.update(_person_spans(model, titled))
    return sorted(found)


#: The spaCy labels `orgs` reads (#1130, owner rulings Q11 (b) and R14):
#: organisations, facilities and countries, cities and states. `LOC`
#: (mountains, rivers, regions) and `NORP` are left out.
ORG_LABELS = ("ORG", "FAC", "GPE")


def org_spans(line: str) -> List[Span]:
    """Spans of `line` that spaCy reads as an `ORG`, `FAC` or `GPE`, on the
    line title-cased only, never as read (owner ruling R14: measured,
    title-casing alone over-removed least). `_title_cased` keeps one
    character for one, so a span of the title-cased text is a span of
    `line`. There is no fallback: with no model this raises.
    """
    model = _nlp()
    with _NLP_LOCK:
        return sorted({(ent.start_char, ent.end_char)
                       for ent in model(_title_cased(line)).ents
                       if ent.label_ in ORG_LABELS})


_FINDERS = {"names": name_spans, "dates": date_spans, "ids": id_spans,
            "orgs": org_spans, "phones": phone_spans,
            "addresses": address_spans, "sites": site_spans}


def spans(line: str, kinds: Iterable[str]) -> Dict[str, List[Span]]:
    """Each asked kind's spans on `line`; a kind not asked is not run."""
    return {kind: _FINDERS[kind](line) for kind in kinds}

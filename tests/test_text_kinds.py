"""The kinds of text a narrowed `burned_in_text` target names (#1121).

The owner's rulings of 2026-10-15, which these tests hold:

- `dates` are date shapes, with field plausibility, and never times of day
  (Q-B13 (a));
- `ids` are runs of letters and digits, inner `-` or `_` allowed, of at
  least 6 characters of which at least 5 are digits (Q-B14 (a));
- `names` are spaCy `PERSON` entities, never `ORG` (Q-B12 (a)), found on
  the line as read and title-cased, plus a `^` between letters. spaCy is
  required: there is no capitalised-word fallback (Q-B11 (b));
- nothing here compares a reading with any patient's values, and the
  module never logs (its spans are of read text).

spaCy runs in no CI job (Q-B18 (a)): the model is a stub here, on both
interpreters.
"""
import ast
import inspect
import threading
import warnings
from types import SimpleNamespace

import pytest

from isocenter import text_kinds


def _texts(line, finder):
    return [line[s:e] for s, e in finder(line)]


# --- dates ------------------------------------------------------------------

@pytest.mark.parametrize("line", [
    "01/02/1970", "1/2/1970", "31/12/1970", "12/31/1970", "01-02-1970",
    "01.02.1970", "01 02 1970", "01/02/70", "1970-01-02", "1970/1/2",
    "2199.12.31", "1800-01-01", "19700102", "01 JAN 1970", "1 January 1970",
    "Jan 1, 1970", "JAN-1970", "jan 1970", "January 5", "5 Sept", "dec. 31 1999",
    "0I/O2/197O", "3l-I2-2OO1",
])
def test_a_date_shape_is_a_date(line):
    assert _texts(line, text_kinds.date_spans) == [line]


@pytest.mark.parametrize("line", [
    "12.5", "3/4", "13/13/2020", "20251399", "12:34", "12:34:56", "10:11:12", "1024",
    "32/01/1970", "01/02/1799", "01/02/2200", "17991231", "22000101", "1970/1/123",
    "01/02/197", "JAN", "MAY BE", "IS/IS/IS", "SOB-SOB-SOB", "01/02-1970",
    "MARCH", "120kV", "5.0MHz",
    # Look-alikes alone, which read as a date once mapped (`01/01/58`,
    # `18000101`): a candidate needs one real digit (review X3 of #1124).
    "OI/OI/SB", "IBOOOIOI",
])
def test_what_is_not_a_date(line):
    assert text_kinds.date_spans(line) == []


def test_a_date_is_found_inside_a_line_and_only_its_span():
    line = "DOE, JANE  F  01/01/1970"
    assert _texts(line, text_kinds.date_spans) == ["01/01/1970"]
    line = "DOB 01 JAN 1970 SE 3"
    assert _texts(line, text_kinds.date_spans) == ["01 JAN 1970"]


def test_a_time_beside_a_date_is_not_taken():
    line = "01/02/1970 10:11:12"
    assert _texts(line, text_kinds.date_spans) == ["01/02/1970"]


def test_a_month_word_inside_a_word_is_not_a_month():
    assert text_kinds.date_spans("MARKER 12") == []
    assert text_kinds.date_spans("DECOMP 1970") == []


# --- ids ----------------------------------------------------------------------

@pytest.mark.parametrize("line", [
    "MRN0012345", "A1234567", "00123456", "123456", "12-345", "AB12345",
    "ACC_2024_0001", "MRNO012345",
])
def test_a_long_digit_run_is_an_id(line):
    assert _texts(line, text_kinds.id_spans) == [line]


@pytest.mark.parametrize("line", [
    "120kV", "5.0MHz", "10cm", "L5-S1", "12345", "ABCDE12", "ABCD1234",
    "1024", "W:1500", "A-1234", "-12345-", "PORTABLE",
])
def test_what_is_not_an_id(line):
    assert text_kinds.id_spans(line) == []


def test_the_id_thresholds_on_both_sides():
    # Six characters, five digits: in. Five characters: out. Six
    # characters, four digits: out.
    assert text_kinds.id_spans("A12345") == [(0, 6)]
    assert text_kinds.id_spans("12345") == []
    assert text_kinds.id_spans("AB1234") == []
    assert text_kinds.id_spans("1234567") == [(0, 7)]


def test_an_id_is_found_inside_a_line():
    line = "ID: MRN0012345 SE 2"
    assert _texts(line, text_kinds.id_spans) == ["MRN0012345"]


def test_inner_separators_belong_to_the_run_and_outer_ones_do_not():
    line = "-12-3456_"
    assert _texts(line, text_kinds.id_spans) == ["12-3456"]


# --- ids in header text (#1146) -----------------------------------------------
# `id_spans(line, ocr=False)` is SCRUB's arm; the default is the pixels'.
# Strings are invented, or quoted from #1146 (`t2_tse_tra_320_p2`,
# `ep2d_diff_b50_400_800_tra_p3`, `P4^P114^S301^I00004`, `0.781250`,
# `310-50-9582`).

def _header_ids(line):
    return text_kinds.id_spans(line, ocr=False)


@pytest.mark.parametrize("line", [
    "t2_tse_tra_320_p2", "ep2d_diff_b50_400_800_tra_p3", "ACC_2024_0001"])
def test_header_ids_spare_protocol_tokens(line):
    """A run joined by `_` and holding a letter is a protocol token, not
    an id. Kills the letter condition dropped (`310-50-9582`, with no
    `_`, is still an id below)."""
    assert _header_ids(line) == []
    assert _header_ids("310-50-9582") == [(0, 11)]
    # Digits joined by `_` with no letter are not a token: the old rule.
    assert _header_ids("4826_153907") == [(0, 11)]


@pytest.mark.parametrize("line, taken", [
    ("Doe^482615", "482615"),                       # quoted in the ruling
    ("P4^P114^S301^I00004", "I00004"),              # quoted: accepted
    ("Kidney^K12^Phase2^S00417", "S00417"),
    ("t2_tse^4826153", "4826153")])
def test_a_caret_neither_joins_nor_protects_an_id(line, taken):
    """Owner ruling R2: only `_` makes a protocol token. A Person Name's
    `^` beside an identifier must not shelter it, so `P4^P114^S301^I00004`
    loses `I00004`, accepted. Kills `^` joining a run again (the whole
    token would be spared, or taken whole) and `^` read as a token's
    joiner in the letter test."""
    assert _texts(line, _header_ids) == [taken]


@pytest.mark.parametrize("line, taken", [
    ("SUBJ_4826153907", "SUBJ_4826153907"),
    ("dwi_512-77-0314", "dwi_512-77-0314"),
    ("run_1234567_b", "run_1234567_b"),
    ("run_739204_b", None)])
def test_header_ids_take_an_ssn_or_seven_digits_inside_a_token(line, taken):
    """Kills the 7-digit exception dropped, and the threshold moved to 6
    (`run_739204_b`) or 8 (`run_1234567_b`). The whole token goes."""
    assert _texts(line, _header_ids) == ([taken] if taken else [])


@pytest.mark.parametrize("line, spans", [
    ("0.781250", []), ("739204.5", []), ("v2.739204", []),
    ("PROCESSED\\0.781250 \\0.781250", []),
    ("AB.739204", [(3, 9)]), ("ID 1234567.", [(3, 10)])])
def test_header_ids_spare_a_decimal_on_either_side(line, spans):
    """A run joined by `.` to a digit is part of a number. A letter before
    the dot (`AB.`) or a full stop at the end is not. Kills the digit
    before the dot dropped, and the trailing side dropped (`739204.5`)."""
    assert _header_ids(line) == spans


def test_the_pixel_arm_is_unchanged():
    """Burned-in `ids` keeps rc21's rule (owner ruling Q7 (a)), so no
    record is read again. Kills the header rule applied to pixels."""
    assert text_kinds.id_spans("t2_tse_tra_320_p2") == [(0, 17)]
    assert text_kinds.id_spans("0.781250") == [(2, 8)]
    assert text_kinds.id_spans("P4^P114^S301^I00004") == [(13, 19)]
    assert text_kinds.id_spans("ACC_2024_0001") == [(0, 13)]
    assert (inspect.signature(text_kinds.id_spans).parameters["ocr"].default
            is True)
    assert text_kinds.spans("0.781250", ["ids"]) == {"ids": [(2, 8)]}


# --- phones, addresses, sites (#1146, owner rulings Q3-Q5 (a)) -----------------
# Invented, in fictional ranges, or quoted from #1146.

@pytest.mark.parametrize("line, taken", [
    ("Questions call (312)555-0147x12", "(312)555-0147x12"),
    ("Contact 312-555-0183 for results", "312-555-0183"),
    ("Tel +1 312 555 0164", "+1 312 555 0164"),
    ("Ph 312.555.0129 ext 204", "312.555.0129 ext 204"),
    ("Callback +44 20 7946 0958", "+44 20 7946 0958"),
    ("Reader line (312) 555-0172", "(312) 555-0172"),
    ("call (606)066-5106x76", "(606)066-5106x76"),   # quoted, #1146
])
def test_a_phone_number_is_a_phone(line, taken):
    assert _texts(line, text_kinds.phone_spans) == [taken]


@pytest.mark.parametrize("line", [
    "Matrix 512 256 1024", "FOV 350 350 1500", "Series 101 102 1003",
    "Dose 1 312 555 0164", "Ratio 3-4", "W:1500 L:400"])
def test_spaces_do_not_separate_a_phone_without_a_plus(line):
    """Owner ruling Q3 (a): no space separators but after `+`. Kills a
    space admitted between the groups."""
    assert text_kinds.phone_spans(line) == []


@pytest.mark.parametrize("line", [
    "Slices 120-240-3600", "Recon 512.512.0001", "kernel (512) 256-1024"])
def test_a_phone_shaped_number_triple_is_taken(line):
    """The cost the docs state: a triple written like a phone number."""
    assert text_kinds.phone_spans(line) != []


@pytest.mark.parametrize("line, taken", [
    ("Terry Lara : 157 Chisholm Trl Thibodaux, LA 70301",
     "157 Chisholm Trl Thibodaux, LA 70301"),             # quoted, #1146
    ("Pt : 41 Willowbend Ct Fairhaven, OR 97999", "41 Willowbend Ct Fairhaven, OR 97999"),
    ("12 Quarry Ln Maplecrest, VT 05999-1234", "12 Quarry Ln Maplecrest, VT 05999-1234"),
    ("Sent to 880 Orchard Ave Brindle, KS 66999", "880 Orchard Ave Brindle, KS 66999"),
])
def test_a_full_address_is_an_address(line, taken):
    assert _texts(line, text_kinds.address_spans) == [taken]


@pytest.mark.parametrize("line", [
    "Home 41 Willowbend Ct", "Lives at 7 Larkspur Dr",          # a street alone
    "4 Phase Abdomen Ct", "2 View Chest Dr", "12 Lead Ecg St",
    "3 PHASE CT LIVER", "2 Contrast Phase Ct, MR 27",
    "41 Willowbend CT Fairhaven, OR 97999",                    # suffix case
])
def test_what_is_not_an_address(line):
    """Owner ruling Q4 (a): the full form only, the suffix case-sensitive.
    Kills the city, state and ZIP made optional, and the suffix read in
    any case."""
    assert text_kinds.address_spans(line) == []


@pytest.mark.parametrize("line, taken", [
    ("PET-CT STUDY at CHAJH", "CHAJH"), ("Performed at HWACMC", "HWACMC"),
    ("t2_tse_tra_320_p2 at HASCC", "HASCC"),                    # quoted
    ("CT ABDOMEN at QRMC", "QRMC"), ("Outside read at BVCHKLMN", "BVCHKLMN"),
    ("Perfusion at REST", "REST"),        # the documented cost of the rule
])
def test_capitals_after_at_are_a_site(line, taken):
    assert _texts(line, text_kinds.site_spans) == [taken]


@pytest.mark.parametrize("line", [
    "Scan at ER", "Nodule at RUL", "Lesion at LLL", "Imaging at MRI",
    "Colo_supine  1.0  B30f at FGH",          # quoted: 3 letters, missed
    "PERFUSION AT REST", "SCAN AT DELAY", "Scanned at NVRHKLMNO",
    "Stenosis at ICA origin", "Seen at Mercy", "Seen at QRMCs",
    "seat QRMC", "t2_tse_tra_320_p2 at HASCC_2", "Seen at QRMC2",
])
def test_what_is_not_a_site(line):
    """Owner ruling Q5 (a): lower-case `at`, then 4 to 8 capitals as a
    whole word. Kills 2 or 3 capitals admitted (`RUL`, `FGH`), `AT`
    read in any case, the upper bound dropped (9 letters), `at`
    matched inside a word (`seat`), and the word's end read without
    `_` or a digit (`HASCC_2` would lose `HASCC` and keep `_2`)."""
    assert text_kinds.site_spans(line) == []


# --- names, with a stub model ---------------------------------------------

class _Ent(SimpleNamespace):
    pass


class FakeNlp:
    """A spaCy stand-in: `ents` fixed per exact input string."""

    meta = {"version": "3.8.0"}

    def __init__(self, ents_by_text):
        self.ents_by_text = ents_by_text
        self.seen = []
        self.locked = []

    def __call__(self, text):
        self.seen.append(text)
        self.locked.append(text_kinds._NLP_LOCK.locked())
        ents = []
        for label, word in self.ents_by_text.get(text, ()):
            start = text.index(word)
            ents.append(_Ent(label_=label, start_char=start,
                             end_char=start + len(word)))
        return SimpleNamespace(ents=ents)


@pytest.fixture
def nlp(monkeypatch):
    def install(ents_by_text):
        fake = FakeNlp(ents_by_text)
        monkeypatch.setattr(text_kinds, "_nlp", lambda: fake)
        return fake
    return install


def test_a_person_entity_is_a_name(nlp):
    nlp({"JANE DOE 01/02/1970": [("PERSON", "JANE DOE")]})
    assert _texts("JANE DOE 01/02/1970", text_kinds.name_spans) == ["JANE DOE"]


def test_an_org_entity_is_not_a_name(nlp):
    nlp({"MERCY HOSPITAL": [("ORG", "MERCY HOSPITAL")]})
    assert text_kinds.name_spans("MERCY HOSPITAL") == []


def test_the_title_cased_run_is_unioned(nlp):
    """`en_core_web_sm` misses most ALL-CAPS names; the second run is
    title-cased, and its spans are kept beside the first's."""
    fake = nlp({"Doe, Jane 1": [("PERSON", "Jane")],
                "DOE, JANE 1": [("PERSON", "DOE")]})
    assert _texts("DOE, JANE 1", text_kinds.name_spans) == ["DOE", "JANE"]
    assert fake.seen == ["DOE, JANE 1", "Doe, Jane 1"]


def test_a_caret_between_letters_is_a_name_with_no_entity(nlp):
    nlp({})
    assert _texts("PT DOE^JANE X", text_kinds.name_spans) == ["DOE^JANE"]


def test_a_capitalised_label_with_no_entity_is_not_a_name(nlp):
    """Red if a capitalised-word fallback creeps back (Q-B11 (b))."""
    nlp({})
    assert text_kinds.name_spans("PORTABLE") == []
    assert text_kinds.name_spans("Portable Chest") == []


def test_title_casing_keeps_the_line_length():
    """A case mapping that changes length (`ß`) would move every span
    found on the title-cased run off the line as read."""
    for line in ("STRAßE DOE", "ﬁne DOE", "İSTANBUL x"):
        assert len(text_kinds._title_cased(line)) == len(line)
    assert text_kinds._title_cased("DOE, JANE") == "Doe, Jane"


def test_every_model_call_is_under_the_lock(nlp):
    fake = nlp({})
    text_kinds.name_spans("DOE JANE")
    assert fake.locked and all(fake.locked)
    assert not text_kinds._NLP_LOCK.locked()


def test_the_lock_is_a_leaf_lock():
    assert isinstance(text_kinds._NLP_LOCK, type(threading.Lock()))


def test_spans_reads_only_the_kinds_asked_for(nlp, monkeypatch):
    calls = []
    monkeypatch.setattr(text_kinds, "_nlp", lambda: calls.append(1))
    found = text_kinds.spans("01/02/1970 MRN0012345", ("dates", "ids"))
    assert found == {"dates": [(0, 10)], "ids": [(11, 21)]}
    assert calls == []


# --- names, unavailable -------------------------------------------------------

def test_the_reason_is_returned_when_the_model_cannot_load(monkeypatch):
    def fail():
        raise OSError("[E050] Can't find model 'en_core_web_sm'")
    monkeypatch.setattr(text_kinds, "_load_model", fail)
    monkeypatch.setattr(text_kinds, "_MODEL", None)
    reason = text_kinds._nlp_unavailable_reason()
    assert reason == "OSError: [E050] Can't find model 'en_core_web_sm'"


def test_success_is_cached_and_a_failure_is_tried_again(monkeypatch):
    attempts = []
    outcomes = [ImportError("No module named 'spacy'"), FakeNlp({})]

    def load():
        attempts.append(1)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(text_kinds, "_load_model", load)
    monkeypatch.setattr(text_kinds, "_MODEL", None)
    assert text_kinds._nlp_unavailable_reason().startswith("ImportError")
    assert text_kinds._nlp_unavailable_reason() is None
    assert text_kinds._nlp_unavailable_reason() is None
    assert len(attempts) == 2


def test_names_by_names_spacy_and_the_model(monkeypatch):
    monkeypatch.setattr(text_kinds, "_MODEL", FakeNlp({}))
    monkeypatch.setattr(text_kinds, "_spacy_version", lambda: "3.8.16")
    assert text_kinds._names_by() == "spacy 3.8.16 en_core_web_sm 3.8.0"


# --- the module ----------------------------------------------------------------

def test_the_module_never_logs_prints_or_warns():
    """Its inputs are read text: a log line of any level is a leak."""
    tree = ast.parse(inspect.getsource(text_kinds))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.name)
            if isinstance(node, ast.ImportFrom):
                names.add(node.module or "")
        if isinstance(node, ast.Call):
            func = node.func
            called = (func.attr if isinstance(func, ast.Attribute)
                      else getattr(func, "id", ""))
            assert called not in {"print", "warn", "debug", "info", "warning",
                                  "error", "exception", "critical", "log",
                                  "get_logger", "getLogger"}, called
    assert "logging" not in names
    assert not any("logger" in name for name in names)


def test_spacy_is_imported_only_inside_the_loader():
    """`spacy` is the optional `nlp` extra: a module-scope import would
    fail on every install without it."""
    tree = ast.parse(inspect.getsource(text_kinds))
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in node.names] + [getattr(node, "module", "") or ""]
            assert not any(m.split(".")[0] in {"spacy", "en_core_web_sm"}
                           for m in mods)


#: spaCy's two warnings that quote text, as `spacy.errors.Warnings` spells
#: them: the code in brackets, then the message.
W030 = '[W030] Some entities could not be aligned in the text "{text}" with entities "x".'
W102 = "[W102] Skipping unsupported user data '{text}: 1' while merging docs."


@pytest.mark.parametrize("module", ["spacy.language", "spacy.training.iob_utils",
                                    "thinc.api", "isocenter.text_kinds"])
@pytest.mark.parametrize("shape", [W030, W102])
def test_a_text_quoting_model_warning_is_not_let_out(monkeypatch, recwarn,
                                                     shape, module):
    """Through the loader, as a real model is reached: W030 and W102, the
    two spaCy warnings that quote text, are ignored while a line is read,
    from spaCy, thinc, or a `stacklevel` pointing into this module
    (review F1 of #1124; owner ruling of 2026-10-15)."""

    class Noisy(FakeNlp):
        def __call__(self, text):
            warnings.warn_explicit(shape.format(text=text), UserWarning,
                                   "model.py", 1, module=module)
            return super().__call__(text)
    monkeypatch.setattr(text_kinds, "_MODEL", None)
    monkeypatch.setattr(text_kinds, "_load_model", lambda: Noisy({}))
    text_kinds.name_spans("QZXJ7SENTINEL")
    assert not [w for w in recwarn if "QZXJ7SENTINEL" in str(w.message)]


@pytest.mark.parametrize("module", ["spacy.language", "thinc.api", "spacyish"])
@pytest.mark.parametrize("message", [
    "[W999] a diagnostic that quotes nothing",
    "[W095] Model 'en_core_web_sm' was trained with another version",
    "W030 without its brackets"])
def test_every_other_model_warning_reaches_the_host(monkeypatch, recwarn,
                                                    message, module):
    """A library does not silence a dependency's diagnostics for its host
    (the 0.9.x pydicom ruling): with the model loaded, every spaCy or
    thinc warning but the two that quote text is shown (owner ruling of
    2026-10-15)."""
    monkeypatch.setattr(text_kinds, "_MODEL", None)
    monkeypatch.setattr(text_kinds, "_load_model", lambda: FakeNlp({}))
    text_kinds.name_spans("QZXJ7")
    warnings.warn_explicit(message, UserWarning, "model.py", 1, module=module)
    assert [str(w.message) for w in recwarn] == [message]


def test_a_text_quoting_code_from_another_library_reaches_the_host(monkeypatch, recwarn):
    """The filter is narrowed by module as well as message: a `[W030]`
    another library raises is that library's diagnostic, and is shown."""
    monkeypatch.setattr(text_kinds, "_MODEL", None)
    monkeypatch.setattr(text_kinds, "_load_model", lambda: FakeNlp({}))
    text_kinds.name_spans("QZXJ7")
    message = W030.format(text="not read by us")
    warnings.warn_explicit(message, UserWarning, "x.py", 1, module="spacyish")
    assert [str(w.message) for w in recwarn] == [message]


def test_a_host_filter_added_after_the_load_keeps_its_place(monkeypatch):
    """The filter is inserted only when missing, so a filter the host adds
    after the model has loaded stays ahead of it: here the host turns
    W030 into an error, and the next line read raises it. A presence
    check that never matched the entry `filterwarnings` writes would put
    the filter back in front on every call and hide the host's choice."""

    class Noisy(FakeNlp):
        def __call__(self, text):
            warnings.warn_explicit(W030.format(text="x"), UserWarning,
                                   "model.py", 1, module="spacy.language")
            return super().__call__(text)
    monkeypatch.setattr(text_kinds, "_MODEL", None)
    monkeypatch.setattr(text_kinds, "_load_model", lambda: Noisy({}))
    with warnings.catch_warnings():
        text_kinds.name_spans("QZXJ7")
        warnings.filterwarnings("error", message=r"\[W030\]")
        with pytest.raises(UserWarning):
            text_kinds.name_spans("QZXJ7")


def test_a_model_first_loaded_inside_a_callers_catch_warnings_stays_quiet(
        monkeypatch, recwarn):
    """A filter inserted inside someone else's `catch_warnings()` block
    goes with the block. The model, loaded there, warned again on the
    next call (review of #1124 at bc02c116). The filter is put back
    whenever it is missing from the filters in force."""

    class Noisy(FakeNlp):
        def __call__(self, text):
            warnings.warn_explicit(W030.format(text=text), UserWarning,
                                   "model.py", 1, module="spacy.language")
            return super().__call__(text)
    monkeypatch.setattr(text_kinds, "_MODEL", None)
    monkeypatch.setattr(text_kinds, "_load_model", lambda: Noisy({}))
    with warnings.catch_warnings():
        text_kinds.name_spans("QZXJ7FIRST")
    text_kinds.name_spans("QZXJ7SECOND")
    assert not [w for w in recwarn if "QZXJ7" in str(w.message)]
    warnings.warn_explicit("from elsewhere", UserWarning, "x.py", 1,
                           module="spacyish")
    assert [w for w in recwarn if "from elsewhere" in str(w.message)]


def test_the_filter_is_looked_for_in_the_list_in_force(monkeypatch):
    """On the free-threaded build warnings are context-aware: inside a
    `catch_warnings()` block the filters in force are the block's, and
    `warnings.filters` is another list. Here the module list holds the
    filter and the block has dropped it (`resetwarnings`); a check that
    read `warnings.filters` saw it, inserted nothing, and the model's
    warning showed. Red only on 3.14t; on 3.12 the two lists are one."""

    class Noisy(FakeNlp):
        def __call__(self, text):
            warnings.warn_explicit(W030.format(text=text), UserWarning,
                                   "model.py", 1, module="spacy.language")
            return super().__call__(text)
    monkeypatch.setattr(text_kinds, "_MODEL", Noisy({}))
    monkeypatch.setattr(warnings, "filters",
                        [text_kinds._MODEL_WARNINGS_FILTER, *warnings.filters])
    assert text_kinds._MODEL_WARNINGS_FILTER[1] is not None
    with warnings.catch_warnings(record=True) as caught:
        warnings.resetwarnings()
        warnings.simplefilter("always")
        text_kinds.name_spans("QZXJ7RESET")
        warnings.warn_explicit("from elsewhere", UserWarning, "x.py", 1,
                               module="spacyish")
    assert [str(w.message) for w in caught] == ["from elsewhere"]


def test_the_module_never_opens_catch_warnings():
    """`catch_warnings` on a threaded path mutates the process-global
    filters on 3.12, dropping sibling threads' warnings (review F1 of
    #1124; the repo's rule beside `imagecodecs_handler`'s fragment walk
    and `io_handlers`' IS read)."""
    tree = ast.parse(inspect.getsource(text_kinds))
    named = {node.attr for node in ast.walk(tree)
             if isinstance(node, ast.Attribute)}
    named |= {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert "catch_warnings" not in named

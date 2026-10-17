"""What `SCRUB` does to one text value (#1130, Clean Descriptors).

`text_scrub.scrub` removes the spans of the kinds a rule names, with a
connecting word directly left of each, and tidies what is left. These are
pure tests of that function and of `text_kinds`' header-text arms; the
session tests are in `test_a_description_keeps_its_text_without_its_identifiers.py`.

The issue's examples are quoted from #1130 and #1131, where they are
public. The clinical corpus is Appendix A of the batch spec, with every
string that could have come from a site's own data replaced by an
invented one of the same shape.
"""
import ast
import inspect
from types import SimpleNamespace

import pytest

from isocenter import config_manager, text_kinds, text_scrub
from isocenter.text_scrub import name_tokens, scrub

from support.scrub_corpus import CLINICAL, ISSUE_ROWS

ALL = ("names", "dates", "ids", "orgs")
NO_MODEL = ("names", "dates", "ids")

@pytest.mark.parametrize("text, expected, names", ISSUE_ROWS)
def test_the_issue_shapes_are_cleaned(text, expected, names):
    assert scrub(text, NO_MODEL, names)[0] == expected


@pytest.mark.parametrize("text", CLINICAL)
def test_clinical_descriptions_are_left_as_they_are(text):
    assert scrub(text, NO_MODEL) == (text, {})


@pytest.mark.parametrize("text, kept", [
    ("Report for Breast", "Report for Breast"),
    ("Imaging at follow-up for 12345678", "Imaging at follow-up"),
    ("CT from 2019-03-14 for review", "CT for review"),
])
def test_a_connector_goes_only_with_a_span_beside_it(text, kept):
    assert scrub(text, NO_MODEL)[0] == kept


def test_a_title_goes_only_with_a_name():
    assert scrub("Seen by Dr. Smith", NO_MODEL, ("SMITH",))[0] == "Seen"
    assert scrub("Dr 12345678", NO_MODEL)[0] == "Dr"


def test_a_connector_is_whole_word():
    """`Comfort 12345678`: `fort` is not `for`."""
    assert scrub("Comfort 12345678", NO_MODEL)[0] == "Comfort"


@pytest.mark.parametrize("target, counts", [
    (["dates", "ids"], {"dates": 1}), (["ids", "dates"], {"ids": 1})])
def test_one_span_found_by_two_kinds_is_counted_once(target, counts):
    assert scrub("MG BIOPSY SPECIMEN 20190314", target) == (
        "MG BIOPSY SPECIMEN", counts)


def test_a_full_name_is_one_name():
    assert scrub("Report for Elena Marsh", NO_MODEL, ("ELENA", "MARSH")) == (
        "Report", {"names": 1})


@pytest.mark.parametrize("text", CLINICAL + [row[0] for row in ISSUE_ROWS] + [
    "A - 1234567 - B", "x, 1234567, y", "for for 1234567", "Dr. Dr. Smith",
    "(1234567) scan", "a/1234567/b", "1234567 1234567 tail"])
def test_scrub_is_a_fixpoint(text):
    once, _ = scrub(text, NO_MODEL, ("SMITH",))
    assert scrub(once, NO_MODEL, ("SMITH",)) == (once, {})


@pytest.mark.parametrize("text, expected", [
    ("A - 1234567 - B", "A - B"), ("x, 1234567, y", "x, y"),
    ("(1234567) scan", "() scan"), ("for 1234567", "")])
def test_the_tidy_rule(text, expected):
    """One separator survives a cut; brackets are not balanced, as stated."""
    assert scrub(text, NO_MODEL)[0] == expected


@pytest.mark.parametrize("text", ["  CT  HEAD. ", "- KNEE -", "Dr."])
def test_a_value_nothing_is_found_in_is_not_tidied(text):
    """The tidy is for a cut. A value with no span keeps its spacing and
    its ends, so a clean value is never rewritten (and never raised)."""
    assert scrub(text, NO_MODEL) == (text, {})


def test_an_emptied_value_is_the_empty_string_here():
    """The pure function returns `""`; the dummy is the scan's job."""
    assert scrub("for 1234567890", NO_MODEL) == ("", {"ids": 1})


@pytest.mark.parametrize("text, changed", [
    ("2.1.10", True), ("Version 3.10.21", True),
    ("0.2.0", False), ("dcmqi 1.2.4", False), ("v2.1.10", False)])
def test_a_dotted_triple_with_a_two_digit_year_is_a_date(text, changed):
    """Owner ruling Q8 (a): one date rule with burned-in text, pinned."""
    assert (scrub(text, ["dates"])[0] != text) is changed


def test_header_dates_do_not_read_confusables():
    assert text_kinds.date_spans("O1/O2/1999", ocr=False) == []
    assert text_kinds.date_spans("2O17O7O7", ocr=False) == []


def test_ocr_dates_still_do():
    """The burned-in path is unchanged: the default is `ocr=True`."""
    assert text_kinds.date_spans("O1/O2/1999") == [(0, 10)]
    assert text_kinds.date_spans("2O17O7O7") == [(0, 8)]
    assert (inspect.signature(text_kinds.date_spans).parameters["ocr"].default
            is True)


def test_name_tokens_skip_prefix_suffix_short_and_dummy_components():
    assert name_tokens(["MARSH^ELENA^Q^Dr^Jr"]) == ("MARSH", "ELENA")
    assert name_tokens(["ANONYMIZED", "ANON_1234", "O^Lee"]) == ("LEE",)
    assert name_tokens(["Smith Jones^Mary=Alpha^Beta\\Other^One"]) == (
        "SMITH", "JONES", "MARY", "ALPHA", "BETA", "OTHER", "ONE")
    assert name_tokens([None, b"BYTES^NAME", 7]) == ()


def test_a_name_is_matched_whole_word_and_in_any_case():
    assert scrub("marsh report", ["names"], ("MARSH",))[0] == "report"
    assert scrub("Marshland report", ["names"], ("MARSH",)) == (
        "Marshland report", {})


def test_the_scrub_kinds_are_one_list():
    assert config_manager.SCRUB_KINDS == text_scrub.SCRUB_KINDS == ALL
    assert set(text_scrub.SCRUB_KINDS) <= set(text_kinds.KINDS)


def test_this_module_never_logs_prints_or_warns():
    """Its input is header text: a log line of any level is a leak."""
    tree = ast.parse(inspect.getsource(text_scrub))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            called = (func.attr if isinstance(func, ast.Attribute)
                      else getattr(func, "id", ""))
            assert called not in {"print", "warn", "debug", "info", "warning",
                                  "error", "exception", "critical", "log",
                                  "get_logger", "getLogger"}, called
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                assert "logg" not in alias.name, alias.name


# --- orgs, with a stand-in model ----------------------------------------------

class _Ent(SimpleNamespace):
    pass


class FakeNlp:
    """A spaCy stand-in: `ents` fixed per exact input string."""

    meta = {"version": "0.0.0"}

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


CLINIC = "LUNG CA ORVIN 4471 at Fernwood Community Clinic"
CLINIC_TITLED = "Lung Ca Orvin 4471 At Fernwood Community Clinic"


def test_orgs_reads_the_title_cased_text_only(nlp):
    fake = nlp({CLINIC_TITLED: [("ORG", "Fernwood Community Clinic")],
                CLINIC: [("ORG", "LUNG CA ORVIN")]})
    assert scrub(CLINIC, ["orgs"]) == ("LUNG CA ORVIN 4471", {"orgs": 1})
    assert fake.seen == [CLINIC_TITLED, "Lung Ca Orvin 4471"]


@pytest.mark.parametrize("label, removed", [
    ("ORG", True), ("FAC", True), ("GPE", True),
    ("LOC", False), ("PERSON", False), ("NORP", False)])
def test_orgs_keeps_only_org_fac_and_gpe(nlp, label, removed):
    nlp({CLINIC_TITLED: [(label, "Fernwood Community Clinic")]})
    assert (scrub(CLINIC, ["orgs"])[0] == "LUNG CA ORVIN 4471") is removed
    assert text_scrub.ORG_LABELS == ("ORG", "FAC", "GPE")


def test_an_org_span_takes_its_connector(nlp):
    nlp({"Scan At North Clinic Today": [("FAC", "North Clinic")]})
    assert scrub("SCAN AT NORTH CLINIC TODAY", ["orgs"])[0] == "SCAN TODAY"


def test_a_cut_leaves_no_space_before_a_comma(nlp):
    """Found measuring `orgs` (R11): `MR , UNIT` must not occur."""
    nlp({"Mr Hepa, Unit At Twmc": [("ORG", "Hepa")]})
    assert scrub("MR HEPA, UNIT at TWMC", ["orgs"])[0] == "MR, UNIT at TWMC"


def test_orgs_runs_under_the_model_lock(nlp):
    fake = nlp({})
    scrub("SOME TEXT", ["orgs"])
    assert fake.locked == [True]
    assert not text_kinds._NLP_LOCK.locked()


def test_dates_and_ids_ask_no_model(monkeypatch):
    def fail():
        raise AssertionError("the model was asked")
    monkeypatch.setattr(text_kinds, "_nlp", fail)
    assert scrub("A 20190314", NO_MODEL, ("X",)) == ("A", {"dates": 1})


@pytest.mark.parametrize("text", ["2O17O7O7", "O1/O2/1999"])
def test_scrub_reads_dates_without_ocr_confusables(text):
    """SCRUB reads header text, typed, never read by OCR: a letter O is not
    a zero there, so these hold no date, although the burned-in reader,
    which reads OCR's confusables, finds one in each."""
    assert text_kinds.date_spans(text), text
    assert scrub(text, ["dates"]) == (text, {})

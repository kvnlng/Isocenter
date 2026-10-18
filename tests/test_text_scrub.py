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

ALL = ("names", "dates", "ids", "orgs", "phones", "addresses", "sites",
       "initials")
NO_MODEL = ("names", "dates", "ids")
REGULAR = NO_MODEL + ("phones", "addresses", "sites", "initials")

@pytest.mark.parametrize("text, expected, names", ISSUE_ROWS)
def test_the_issue_shapes_are_cleaned(text, expected, names):
    assert scrub(text, NO_MODEL, names)[0] == expected


@pytest.mark.parametrize("text", CLINICAL)
def test_clinical_descriptions_are_left_as_they_are(text):
    assert scrub(text, NO_MODEL) == (text, {})


@pytest.mark.parametrize("text, kept", [
    ("Report for Breast", "Report for Breast"),
    ("Imaging at follow-up for 12345678", "Imaging at follow-up"),
    ("CT from 2017-07-07 for review", "CT for review"),
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
    assert scrub("MA SURGICAL SPECIMEN 20170707", target) == (
        "MA SURGICAL SPECIMEN", counts)


def test_a_full_name_is_one_name():
    assert scrub("Report for Elena Marsh", NO_MODEL, ("ELENA", "MARSH")) == (
        "Report", {"names": 1})


@pytest.mark.parametrize("text", CLINICAL + [row[0] for row in ISSUE_ROWS] + [
    "A - 1234567 - B", "x, 1234567, y", "for for 1234567", "Dr. Dr. Smith",
    "(1234567) scan", "a/1234567/b", "1234567 1234567 tail",
    "AP Routine  5.0  B40f 20190901", "Thorax  1.0  B45f for 4826153907",
    "for 4826153907 Brain MRI.", "Line one\n4826153907\nLine three",
    "Ordered for 2675888487 by SS", "PET-CT STUDY at CHAJH",
    "x 4826153907 , y"])
def test_scrub_is_a_fixpoint(text):
    once, _ = scrub(text, REGULAR, ("SMITH",))
    assert scrub(once, REGULAR, ("SMITH",)) == (once, {})


@pytest.mark.parametrize("text", CLINICAL)
def test_the_regular_kinds_leave_clinical_descriptions_as_they_are(text):
    """None of the new kinds takes anything from the clinical corpus."""
    assert scrub(text, REGULAR) == (text, {})


@pytest.mark.parametrize("text, expected", [
    ("A - 1234567 - B", "A - B"), ("x, 1234567, y", "x, y"),
    ("(1234567) scan", "() scan"), ("for 1234567", "")])
def test_the_tidy_rule(text, expected):
    """One separator survives a cut; brackets are not balanced, as stated."""
    assert scrub(text, NO_MODEL)[0] == expected


@pytest.mark.parametrize("text, expected", [
    ("AP Routine  5.0  B40f 20190901", "AP Routine  5.0  B40f"),   # quoted, #1146
    ("Thorax  1.0  B45f for 4826153907", "Thorax  1.0  B45f"),
    ("for 4826153907 Brain MRI.", "Brain MRI."),
    ("  Brain MRI for 4826153907", "  Brain MRI"),
    ("Line one\n4826153907\nLine three", "Line one\nLine three"),
    ("Line one\r\n4826153907 Line three", "Line one\r\nLine three"),
    ("x 4826153907 , y", "x, y"),
    ("A  4826153907  B", "A B")])
def test_the_tidy_is_only_at_a_cut(text, expected):
    """#1146 (3): whitespace away from a cut is the text's own, and an end
    a cut did not reach keeps its characters. At a cut the whitespace
    becomes one space, or its first line break. Kills the global collapse
    restored (rows 1 and 2), both ends stripped whatever the cut (rows 3
    and 4), the joint always a space (rows 5 and 6), and the comma arm
    not eating the whitespace on its right (row 7)."""
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
    """Every kind but `initials` is a `text_kinds` finder; `initials` is a
    widening of a span another kind found, so it has none (#1146)."""
    assert config_manager.SCRUB_KINDS == text_scrub.SCRUB_KINDS == ALL
    assert set(text_scrub.SCRUB_KINDS) - {"initials"} <= set(text_kinds.KINDS)
    assert "initials" not in text_kinds.KINDS
    # Q-B12 stands for pixels: none of the new kinds is a burned-in kind.
    for kind in ("phones", "addresses", "sites", "initials"):
        assert kind not in config_manager.BURNED_IN_TEXT_KINDS


# --- ids in header text, and a name cut at the VR's length (#1146) ------------

@pytest.mark.parametrize("text, expected", [
    ("ep2d_diff_b50_400_800_tra_p3 for 310-50-9582",            # quoted
     "ep2d_diff_b50_400_800_tra_p3"),
    ("P4^P114^S301^I00004, Gated, 10.0%A for 337-04-5046",      # quoted
     "P4^P114^S301^I00004, Gated, 10.0%A"),
    ("t2_tse_tra_320_p2 for 4826153907", "t2_tse_tra_320_p2"),
    ("PROCESSED\\0.781250 \\0.781250", "PROCESSED\\0.781250 \\0.781250"),  # quoted
    ("Kidney^K12^Phase2^S00417 for 4826153907", "Kidney^K12^Phase2^S00417"),
    ("Pixel spacing 0.488281 \\0.488281", "Pixel spacing 0.488281 \\0.488281"),
    ("Run_A 12.718204 mm", "Run_A 12.718204 mm"),
    ("SUBJ_4826153907 baseline", "baseline"),
    ("dwi_b800_tra for 512-77-0314", "dwi_b800_tra")])
def test_scrub_reads_ids_without_the_pixel_rule(text, expected):
    """#1146 (4): SCRUB asks `id_spans(text, ocr=False)`. Kills `ids`
    still routed through `text_kinds.spans`, the pixels' arm."""
    assert scrub(text, NO_MODEL)[0] == expected


FENWICK = ("FENWICK-ORR", "DALIA")
CUT = "t1_mprage_sag_iso_1mm_p2_ND_RR_MOCO_SUMMARY_TRA for Dalia Fenwic"


@pytest.mark.parametrize("text, names, max_length, expected", [
    (CUT, FENWICK, 64, "t1_mprage_sag_iso_1mm_p2_ND_RR_MOCO_SUMMARY_TRA"),
    # Quoted, #1146: a Series Description cut at LO's 64.
    ("ep2d_diff_b50_400_800_tra_p3_TRACEW_DFC_MIX for Christopher Powe",
     ("POWERS", "CHRISTOPHER"), 64, "ep2d_diff_b50_400_800_tra_p3_TRACEW_DFC_MIX"),
    # One short of the limit: not cut, so `Fenwic` is a word, not a name.
    ("t1_mprage_sag_iso_1mm_p2_ND_RR_MOCO_SUMMARY_TR for Dalia Fenwic",
     FENWICK, 64, "t1_mprage_sag_iso_1mm_p2_ND_RR_MOCO_SUMMARY_TR Fenwic"),
    # Two characters are too few.
    ("t1_mprage_sag_iso_1mm_p2_ND_RR_MOCO_SUMMARY_TRA_SAG for Dalia Fe",
     FENWICK, 64, "t1_mprage_sag_iso_1mm_p2_ND_RR_MOCO_SUMMARY_TRA_SAG Fe"),
    # A protocol segment is not a word: only whitespace may precede it.
    ("t2_tse_cor_384_p2_fs_BH_ND_RR_MOCO_SUMMARY_COMPOSED_SER_1717_tra",
     ("TRANH", "MINH"), 64,
     "t2_tse_cor_384_p2_fs_BH_ND_RR_MOCO_SUMMARY_COMPOSED_SER_1717_tra"),
    ("Report for Fenw", FENWICK, 64, "Report for Fenw"),
    (CUT, FENWICK, None, "t1_mprage_sag_iso_1mm_p2_ND_RR_MOCO_SUMMARY_TRA Fenwic"),
])
def test_a_name_cut_at_the_limit_is_removed(text, names, max_length, expected):
    """#1146 (2): a value exactly at its VR's maximum whose last word, 3 or
    more letters, begins one of the instance's names. Kills the length
    gate removed or loosened (the 63-character row), the floor lowered to
    2 (the `Fe` row), and the boundary loosened to admit `_` (the `_tra`
    row)."""
    cleaned, counts = scrub(text, NO_MODEL, names, max_length=max_length)
    assert cleaned == expected
    if expected != text and len(text) == max_length:
        assert counts == {"names": 1}, counts


def test_the_cut_rows_are_the_lengths_they_say():
    assert len(CUT) == 64
    assert len("ep2d_diff_b50_400_800_tra_p3_TRACEW_DFC_MIX for Christopher Powe") == 64
    assert len("t1_mprage_sag_iso_1mm_p2_ND_RR_MOCO_SUMMARY_TR for Dalia Fenwic") == 63
    assert len("t1_mprage_sag_iso_1mm_p2_ND_RR_MOCO_SUMMARY_TRA_SAG for Dalia Fe") == 64
    assert len("t2_tse_cor_384_p2_fs_BH_ND_RR_MOCO_SUMMARY_COMPOSED_SER_1717_tra") == 64


def test_a_cut_name_is_read_only_in_the_first_round():
    """Only the value as read can have been cut at its VR's length: a
    scrubbed value read again, even with the limit set to its own length,
    loses nothing more. (Within one call a later round's value is always
    shorter than the first's, since a round that changes nothing ends the
    loop, so the gate never holds there; the round counter says so.)"""
    once, _ = scrub(CUT, NO_MODEL, FENWICK, max_length=64)
    assert scrub(once, NO_MODEL, FENWICK, max_length=len(once)) == (once, {})


# --- the new kinds (#1146, owner rulings Q3-Q6 (a)) ---------------------------

@pytest.mark.parametrize("text, kinds, expected, counts", [
    ("Questions call (312)555-0147x12", ["phones"], "Questions call", {"phones": 1}),
    ("call (606)066-5106x76", ["phones", "ids"], "call", {"phones": 1}),  # quoted
    ("call (606)066-5106x76", ["ids"], "call (606)", {"ids": 1}),         # quoted, rc21
    ("Terry Lara : 157 Chisholm Trl Thibodaux, LA 70301",                  # quoted
     ["names", "addresses"], "", {"names": 1, "addresses": 1}),
    ("PET-CT STUDY at CHAJH", ["sites"], "PET-CT STUDY", {"sites": 1}),    # quoted
    ("Performed at HWACMC", ["sites"], "Performed", {"sites": 1}),         # quoted
    ("Ordered for 2675888487 by SS", ["ids", "initials"], "Ordered",       # quoted
     {"ids": 1, "initials": 1}),
    ("Ordered for 2675888487 by SS", ["ids"], "Ordered by SS", {"ids": 1}),
])
def test_the_new_kinds_remove_their_shapes(text, kinds, expected, counts):
    names = ("TERRY", "LARA")
    assert scrub(text, kinds, names) == (expected, counts)


@pytest.mark.parametrize("text", [
    "Ordered by SS", "Confirmed by CT", "Seen by ENT", "Read by MR today",
    "Contrast by IV for review"])
def test_initials_go_only_with_a_span_removed_beside_them(text):
    """Owner ruling Q6 (a): `by` and 1 to 3 capitals only directly right of
    a span the same round removes. Kills a free-standing `by SS` taken."""
    assert scrub(text, ["ids", "dates", "initials"]) == (text, {})


@pytest.mark.parametrize("text, expected", [
    ("Ordered 20170803 by SS today", "Ordered today"),
    ("Ordered for 2675888487 BY SS", "Ordered BY SS"),
    ("Ordered for 2675888487 by SSSS", "Ordered by SSSS"),
    ("Ordered for 2675888487 by Smith", "Ordered by Smith"),
    ("Ordered for 2675888487, by SS", "Ordered, by SS"),
])
def test_initials_shape(text, expected):
    """Lower-case `by`, then 1 to 3 capitals as a whole word, directly
    right of the span (whitespace only between)."""
    assert scrub(text, ["dates", "ids", "initials"])[0] == expected


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


CLINIC = "LUNG CA ACCRIN 6668 at Harris Community Clinic"
CLINIC_TITLED = "Lung Ca Accrin 6668 At Harris Community Clinic"


def test_orgs_reads_the_title_cased_text_only(nlp):
    fake = nlp({CLINIC_TITLED: [("ORG", "Harris Community Clinic")],
                CLINIC: [("ORG", "LUNG CA ACCRIN")]})
    assert scrub(CLINIC, ["orgs"]) == ("LUNG CA ACCRIN 6668", {"orgs": 1})
    assert fake.seen == [CLINIC_TITLED, "Lung Ca Accrin 6668"]


@pytest.mark.parametrize("label, removed", [
    ("ORG", True), ("FAC", True), ("GPE", True),
    ("LOC", False), ("PERSON", False), ("NORP", False)])
def test_orgs_keeps_only_org_fac_and_gpe(nlp, label, removed):
    nlp({CLINIC_TITLED: [(label, "Harris Community Clinic")]})
    assert (scrub(CLINIC, ["orgs"])[0] == "LUNG CA ACCRIN 6668") is removed
    assert text_scrub.ORG_LABELS == ("ORG", "FAC", "GPE")


def test_an_org_span_takes_its_connector(nlp):
    nlp({"Scan At North Clinic Today": [("FAC", "North Clinic")]})
    assert scrub("SCAN AT NORTH CLINIC TODAY", ["orgs"])[0] == "SCAN TODAY"


def test_a_cut_leaves_no_space_before_a_comma(nlp):
    """Found measuring `orgs` (R11): `MR , UNIT` must not occur."""
    nlp({"Mr Breas, Unit At Msch": [("ORG", "Breas")]})
    assert scrub("MR BREAS, UNIT at MSCH", ["orgs"])[0] == "MR, UNIT at MSCH"


def test_orgs_runs_under_the_model_lock(nlp):
    fake = nlp({})
    scrub("SOME TEXT", ["orgs"])
    assert fake.locked == [True]
    assert not text_kinds._NLP_LOCK.locked()


def test_dates_and_ids_ask_no_model(monkeypatch):
    def fail():
        raise AssertionError("the model was asked")
    monkeypatch.setattr(text_kinds, "_nlp", fail)
    assert scrub("A 20170707", NO_MODEL, ("X",)) == ("A", {"dates": 1})


@pytest.mark.parametrize("text", ["2O17O7O7", "O1/O2/1999"])
def test_scrub_reads_dates_without_ocr_confusables(text):
    """SCRUB reads header text, typed, never read by OCR: a letter O is not
    a zero there, so these hold no date, although the burned-in reader,
    which reads OCR's confusables, finds one in each."""
    assert text_kinds.date_spans(text), text
    assert scrub(text, ["dates"]) == (text, {})

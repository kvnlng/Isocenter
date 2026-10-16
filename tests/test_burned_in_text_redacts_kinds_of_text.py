"""`burned_in_text` `target: any | [names, dates, ids]` (#1121, PR B).

The owner's rulings of 2026-10-15, which these tests hold:

- `target` is optional and defaults to `any` (Q-B1 (a)). Narrowed, it
  names kinds of text, and a word OCR read is redacted when a span of a
  named kind overlaps it (Q-B15 (a)): never the whole line. Nothing is
  compared with any patient's values;
- a narrowed target never redacts a region `any` would not, so
  `min_confidence` keeps its one meaning (Q-B17 (a)) and every guarantee
  of `any` (scope, the two-letters-or-digits rule, padding) stays;
- `names` needs spaCy and `en_core_web_sm`; with them unavailable,
  `redact()` and `export(check_burned_in=True)` raise
  `NlpUnavailableError`, an `OcrUnavailableError`, where OCR's check
  sits, never at load (Q-B11 (b)). Plain `export()` reads records and
  needs no model;
- `names_by`, the model's identity, lives in the record and not in the
  policy hash: `redact()` reads an image again when the model loaded now
  is another (owner ruling (a) of 2026-10-15 on the developer's question);
- one `RISK` row per image with text left in place (Q-B16 (a)); the
  residual read counts only text of a named kind;
- nothing read is carried in a row, log, warning, exception, the store,
  the record or a hash.

OCR and spaCy are stand-ins here, threads only: the redact tests'
`FakeTesseract` finds ink and reports it as one word, extended below to
report several words on lines.
"""
import itertools
import json
import logging
import sqlite3
import warnings

import numpy as np
import pytest
import yaml

from isocenter import config_manager, pixel_analysis, services, text_kinds
from isocenter import session as session_module
from isocenter.config_manager import ConfigLoader
from isocenter.reporting import BurnedInTextSummary, ComplianceReport
from isocenter.session import DicomSession as Session

from test_redact_blanks_burned_in_text import (
    COLS, INK, ROWS, FakeTesseract, audit_rows, exported_files, write_image)
from test_text_kinds import FakeNlp


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _write(tmp_path, text, name="c.yaml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _refusal(tmp_path, text):
    path = _write(tmp_path, text)
    with pytest.raises(ValueError) as caught:
        ConfigLoader.load_unified_config(str(path))
    return str(path), str(caught.value)


# --- the loader ---------------------------------------------------------------

_BASE = "{action: redact, scope: all, "


@pytest.mark.parametrize("target, reason", [
    ("names", "'burned_in_text' target 'names' must be any or a list of "
              "kinds such as [names, dates, ids]"),
    ("Any", "'burned_in_text' target 'Any' must be any or a list of kinds "
            "such as [names, dates, ids]"),
    ("3", "'burned_in_text' target 3 must be any or a list of kinds such as "
          "[names, dates, ids]"),
    ("null", "'burned_in_text' target None must be any or a list of kinds "
             "such as [names, dates, ids]"),
    ("[]", "'burned_in_text' target is an empty list; name at least one of "
           "names, dates, ids, or write any"),
    ("[Names]", "'burned_in_text' target item 'Names' is not one of names, "
                "dates, ids (exact spelling)"),
    ("[dates, 7]", "'burned_in_text' target item 7 is not one of names, "
                   "dates, ids (exact spelling)"),
    ("[any, names]", "'burned_in_text' target lists any beside other kinds; "
                     "write target: any alone"),
    ("[any]", "'burned_in_text' target item 'any' is not one of names, "
              "dates, ids (exact spelling)"),
], ids=["bare-kind", "case", "number", "null", "empty", "item-case",
        "item-number", "any-beside", "any-alone"])
def test_each_target_refusal_says_one_sentence(tmp_path, target, reason):
    path, message = _refusal(tmp_path, f"burned_in_text: {_BASE}target: {target}}}\n")
    assert message == f"{path}: {reason}"


def test_the_target_is_judged_after_the_scope_and_before_the_threshold(tmp_path):
    path, message = _refusal(
        tmp_path, "burned_in_text: {action: redact, scope: modalities, "
                  "target: [], min_confidence: 900}\n")
    assert "needs 'modalities'" in message
    path, message = _refusal(
        tmp_path, f"burned_in_text: {_BASE}target: [], min_confidence: 900}}\n")
    assert message == (f"{path}: 'burned_in_text' target is an empty list; "
                       f"name at least one of names, dates, ids, or write any")


@pytest.mark.parametrize("target, normalised", [
    (None, "any"), ("any", "any"), ("[dates, names, dates]", ["dates", "names"]),
    ("[ids, names, dates]", ["dates", "ids", "names"]),
], ids=["absent", "any", "repeat", "order"])
def test_the_target_is_normalised(tmp_path, target, normalised):
    body = _BASE + (f"target: {target}}}" if target else "}")
    parts = ConfigLoader.load_unified_config(
        str(_write(tmp_path, f"burned_in_text: {body}\n")))
    assert parts[5] == {"action": "redact", "scope": "all",
                        "min_confidence": 80, "target": normalised}


@pytest.mark.parametrize("policy", [
    {"action": "redact", "scope": "all", "min_confidence": 80, "target": "any"},
    {"action": "report", "scope": "flagged", "min_confidence": 70,
     "target": ["dates", "ids"]},
], ids=["any", "narrowed"])
def test_save_and_create_config_write_the_target(tmp_path, policy):
    path = _write(tmp_path, "burned_in_text: " + json.dumps(policy) + "\n")
    with Session(str(tmp_path / "s.db")) as session:
        session.load_config(str(path))
        assert session.configuration.burned_in_text == policy
        session.configuration.save()
        assert yaml.safe_load(path.read_text())["burned_in_text"] == policy
        out = tmp_path / "scaffold.yaml"
        session.create_config(str(out))
        assert yaml.safe_load(out.read_text())["burned_in_text"] == policy


def test_the_kinds_are_one_list_in_two_modules():
    assert config_manager.BURNED_IN_TEXT_KINDS == text_kinds.KINDS == (
        "names", "dates", "ids")
    assert "target" in config_manager._BURNED_IN_TEXT_KEYS


# --- the read, with stand-ins -------------------------------------------------

#: The words drawn, `(text, x, y, w, h, line)`: two lines on a 64x96
#: frame, each word's box filled with ink so the stand-in finds it while
#: any of it is left.
NAME = ("DOE,", 2, 4, 12, 8, 1)
GIVEN = ("JANE", 18, 4, 16, 8, 1)
DOB = ("01/01/1970", 40, 4, 30, 8, 1)
LABEL = ("PORTABLE", 2, 30, 28, 8, 2)
MRN = ("MRN0012345", 40, 30, 40, 8, 2)
WORDS = (NAME, GIVEN, DOB, LABEL, MRN)


def _padded(word):
    _text, x, y, w, h, _line = word
    pad = max(2, round(0.15 * h))
    return (0, max(0, y - pad), min(ROWS, y + h + pad),
            max(0, x - pad), min(COLS, x + w + pad))


class WordsTesseract(FakeTesseract):
    """Reports each word whose box still holds ink, on its line."""

    def __init__(self, words=WORDS, conf=96):
        super().__init__(conf=conf)
        self.words = list(words)

    def image_to_data(self, img, *args, **kwargs):
        self.calls += 1
        if self.fail or self.calls in self.fail_calls:
            raise RuntimeError(self.fail_message)
        arr = np.asarray(img)
        found = [w for w in self.words
                 if (arr[w[2]:w[2] + w[4], w[1]:w[1] + w[3]] == INK).any()]
        return {"text": [w[0] for w in found],
                "conf": [self.conf] * len(found),
                "left": [w[1] for w in found], "top": [w[2] for w in found],
                "width": [w[3] for w in found], "height": [w[4] for w in found],
                "block_num": [w[5] for w in found],
                "par_num": [1] * len(found), "line_num": [1] * len(found)}


def _ink(words=WORDS):
    return [(0, y, y + h, x, x + w) for _t, x, y, w, h, _l in words]


@pytest.fixture
def words_ocr(monkeypatch):
    fake = WordsTesseract()
    monkeypatch.setattr(pixel_analysis, "HAS_OCR", True)
    monkeypatch.setattr(pixel_analysis, "pytesseract", fake)
    return fake


@pytest.fixture
def nlp(monkeypatch):
    """A stand-in model that reads `DOE, JANE` as a person only
    title-cased, as `en_core_web_sm` misses most upper-case names."""
    fake = FakeNlp({"Doe, Jane 01/01/1970": [("PERSON", "Doe, Jane")]})
    monkeypatch.setattr(text_kinds, "_MODEL", None)
    monkeypatch.setattr(text_kinds, "_load_model", lambda: fake)
    monkeypatch.setattr(text_kinds, "_spacy_version", lambda: "3.8.16")
    return fake


def _no_model():
    raise ImportError("No module named 'spacy'")


@pytest.fixture
def no_nlp(monkeypatch):
    """spaCy as on 3.14t: the import fails."""
    monkeypatch.setattr(text_kinds, "_MODEL", None)
    monkeypatch.setattr(text_kinds, "_load_model", _no_model)


UID = "1.2.826.0.1.3680043.8.498.1121.20"


def _cfg(tmp_path, action="redact", target=None, name="c.yaml"):
    line = f"  target: {target}\n" if target else ""
    return str(_write(tmp_path, "privacy_profile: basic\nburned_in_text:\n"
                                f"  action: {action}\n  scope: all\n{line}", name))


def _session_with(tmp_path, words=WORDS, uid=UID, **kwargs):
    src = tmp_path / "in"
    src.mkdir(exist_ok=True)
    write_image(src / "a.dcm", sop_uid=uid, ink=_ink(words), **kwargs)
    session = Session(str(tmp_path / "s.db"))
    session.ingest(str(src))
    return session


def _instance(session):
    return session.store.patients[0].studies[0].series[0].instances[0]


def _record(inst):
    return services.burned_in_text_record(inst)


def _blank(arr, word):
    _f, y1, y2, x1, x2 = _padded(word)
    return not (arr[y1:y2, x1:x2] == INK).any()


def test_a_narrowed_target_blanks_only_the_words_of_its_kinds(tmp_path, words_ocr, nlp):
    """The name block and the date of birth go; the label and the MRN
    stay, under `[names, dates]`. Kills the target ignored (all five
    blanked) and the title-cased run dropped (the stub reads the name
    only title-cased)."""
    db = tmp_path / "s.db"
    with _session_with(tmp_path) as session:
        session.load_config(_cfg(tmp_path, target="[names, dates]"))
        assert session.redact(show_progress=False) == 1
        arr = _instance(session).get_pixel_data()
        assert [_blank(arr, w) for w in WORDS] == [True, True, True, False, False]
        record = _record(_instance(session))
        assert record["counts"] == {"names": 2, "dates": 1, "left": 2}
        assert record["names_by"] == "spacy 3.8.16 en_core_web_sm 3.8.0"
    risk = [r for r in audit_rows(db) if r[0] == "RISK"]
    assert len(risk) == 1
    uid = risk[0][1]
    assert risk[0][2] == (f"burned_in_text: 2 text region(s) on frame(s) 0 of "
                          f"instance {uid} were read as none of names, dates "
                          f"(target) and left in place.")


def test_only_the_overlapped_words_go_never_the_whole_line(tmp_path, words_ocr):
    """`[dates]` on a line holding a name and a date: the date's word goes,
    the name's two words on the same line stay (owner ruling Q-B15 (a))."""
    with _session_with(tmp_path) as session:
        session.load_config(_cfg(tmp_path, target="[dates]"))
        session.redact(show_progress=False)
        arr = _instance(session).get_pixel_data()
        assert [_blank(arr, w) for w in WORDS] == [False, False, True, False, False]


def test_under_any_every_word_goes(tmp_path, words_ocr):
    db = tmp_path / "s.db"
    with _session_with(tmp_path) as session:
        session.load_config(_cfg(tmp_path))
        session.redact(show_progress=False)
        arr = _instance(session).get_pixel_data()
        assert all(_blank(arr, w) for w in WORDS)
        assert "counts" not in _record(_instance(session))
        assert "names_by" not in _record(_instance(session))
    assert not [r for r in audit_rows(db) if r[0] == "RISK"]


def test_ids_take_the_record_number_and_leave_the_label(tmp_path, words_ocr):
    with _session_with(tmp_path) as session:
        session.load_config(_cfg(tmp_path, target="[ids]"))
        session.redact(show_progress=False)
        arr = _instance(session).get_pixel_data()
        assert [_blank(arr, w) for w in WORDS] == [False, False, False, False, True]


# --- the invariant --------------------------------------------------------------

_READINGS = [
    WORDS,
    (("19700101", 2, 4, 20, 8, 1), ("SE", 30, 4, 6, 8, 1),
     ("12345678", 40, 4, 24, 8, 1), ("10cm", 2, 30, 12, 8, 2),
     ("DOE^JANE", 20, 30, 30, 8, 2)),
    (("JAN", 2, 4, 10, 8, 1), ("1970", 16, 4, 14, 8, 1),
     ("L", 40, 4, 4, 8, 1), ("120kV", 50, 4, 20, 8, 1),
     ("A1234567", 2, 30, 30, 8, 2), ("Doe", 40, 30, 12, 8, 2)),
    # One-character words inside a match: `5 JAN 1970` is a date whose
    # day is one digit, and `01 / 01 / 1970` read with the slashes as
    # words of their own. Neither `5` nor `/` is a box under `any`.
    (("5", 2, 4, 4, 8, 1), ("JAN", 10, 4, 12, 8, 1), ("1970", 26, 4, 16, 8, 1),
     ("01", 2, 30, 8, 8, 2), ("/", 12, 30, 3, 8, 2), ("01", 18, 30, 8, 8, 2),
     ("/", 28, 30, 3, 8, 2), ("1970", 34, 30, 16, 8, 2)),
]


@pytest.mark.parametrize("words", _READINGS,
                         ids=["block", "compact", "month", "one-character"])
def test_a_narrowed_target_never_boxes_what_any_would_not(tmp_path, monkeypatch, nlp, words):
    """The invariant (spec §4.1), over every subset of the kinds: the boxes
    a narrowed target keeps are a subset of the boxes `any` keeps, at the
    same threshold, above and below it. Red if classification ever adds a
    region: a word under the threshold (the reading at 79), or a word the
    two-letters rule drops that sits inside a match (`5` in `5 JAN 1970`,
    review X1 of #1124; the `L` of the month reading sits outside every
    match and could not show it)."""
    fake = WordsTesseract(words)
    monkeypatch.setattr(pixel_analysis, "HAS_OCR", True)
    monkeypatch.setattr(pixel_analysis, "pytesseract", fake)
    nlp.ents_by_text["Doe"] = [("PERSON", "Doe")]
    with _session_with(tmp_path, words) as session:
        inst = _instance(session)
        for conf in (96, 79):
            fake.conf = conf
            every = set(pixel_analysis._text_boxes(inst, 80).boxes)
            for n in range(1, 4):
                for kinds in itertools.combinations(("names", "dates", "ids"), n):
                    kept = set(pixel_analysis._text_boxes(
                        inst, 80, (), sorted(kinds)).boxes)
                    assert kept <= every, (conf, kinds)
                    if conf == 96 and n == 3:
                        assert kept, "every reading holds a word of some kind"
            if conf == 96:
                assert every


# --- lines are grouped by where the words sit (review F3 of #1124) -----------

def _region(text, x, y, w=10, h=8, frame=0):
    return pixel_analysis.TextRegion(text=text, box=(x, y, w, h),
                                     confidence=96.0, frame_index=frame)


def _kept(regions, kinds):
    kept, counts, _left = pixel_analysis._regions_of_kinds(regions, kinds)
    return [r.text for r in kept], counts


def test_a_line_tesseract_split_across_blocks_is_still_one_line(tmp_path, monkeypatch, nlp):
    """`--psm 11` can put `DOE,` and `JANE` of one visual line in two
    blocks; read apart, neither is a name and the name stays. Grouped by
    where they sit, the name is redacted. Red under tesseract's
    `(block, par, line)` grouping."""
    fake = WordsTesseract(words=[("DOE,", 2, 4, 12, 8, 1), ("JANE", 18, 4, 16, 8, 2),
                                 ("01/01/1970", 40, 4, 30, 8, 3)])
    monkeypatch.setattr(pixel_analysis, "HAS_OCR", True)
    monkeypatch.setattr(pixel_analysis, "pytesseract", fake)
    with _session_with(tmp_path, fake.words) as session:
        session.load_config(_cfg(tmp_path, target="[names]"))
        session.redact(show_progress=False)
        arr = _instance(session).get_pixel_data()
        assert [_blank(arr, w) for w in fake.words] == [True, True, False]


def test_words_on_different_frames_never_share_a_line():
    """`JAN` on frame 0 and `1970` on frame 1, at one height: apart, no
    date; joined across frames, a date that is on no frame (review X6)."""
    regions = [_region("JAN", 2, 4, frame=0), _region("1970", 16, 4, frame=1)]
    assert _kept(regions, ["dates"]) == ([], {"dates": 0, "left": 2})
    one_frame = [_region("JAN", 2, 4), _region("1970", 16, 4)]
    assert _kept(one_frame, ["dates"])[0] == ["JAN", "1970"]


def test_a_line_is_read_left_to_right_whatever_order_tesseract_gave():
    regions = [_region("1970", 40, 4), _region("JAN", 20, 5), _region("01", 2, 3)]
    assert _kept(regions, ["dates"]) == (["01", "JAN", "1970"],
                                         {"dates": 3, "left": 0})


def test_words_share_a_line_only_when_their_heights_overlap_by_half():
    """Overlapping by half the shorter height is one line; less is two.
    `01 JAN` above `1970` is no date; beside it, it is."""
    beside = [_region("01 JAN", 2, 4, w=20), _region("1970", 30, 8)]
    assert _kept(beside, ["dates"])[0] == ["01 JAN", "1970"]
    below = [_region("01 JAN", 2, 4, w=20), _region("1970", 30, 9)]
    assert _kept(below, ["dates"])[0] == ["01 JAN"]


def test_a_word_of_the_line_below_does_not_land_inside_a_name(nlp):
    """A large-font `DOE, JANE` and a small `PORTABLE` below whose box
    reaches into it by more than half its own height: joined as one band
    the line read `DOE, PORTABLE JANE`, which is no name, and the name
    was left (review of #1124 at bc02c116). `PORTABLE` sits under the
    row's words, so it is another line."""
    nlp.ents_by_text["DOE, JANE"] = [("PERSON", "DOE, JANE")]
    regions = [_region("DOE,", 0, 0, w=40, h=30), _region("JANE", 60, 0, w=40, h=30),
               _region("PORTABLE", 30, 24, w=25, h=8)]
    assert _kept(regions, ["names"]) == (["DOE,", "JANE"], {"names": 2, "left": 1})


def test_a_small_label_below_a_large_date_does_not_split_it():
    """Mixed sizes: `01 JAN 1970` 30 px high, `PORTABLE` 8 px high
    reaching 5 px into it; the band read `01 PORTABLE JAN 1970` and the
    `01` was left."""
    regions = [_region("01", 0, 0, w=30, h=30), _region("JAN", 40, 0, w=30, h=30),
               _region("1970", 80, 0, w=40, h=30),
               _region("PORTABLE", 20, 25, w=40, h=8)]
    assert _kept(regions, ["dates"]) == (["01", "JAN", "1970"],
                                         {"dates": 3, "left": 1})


def test_a_line_set_tight_under_another_stays_its_own(nlp):
    """Same height, the second line 4 px lower (6 of 10 px shared): `SE`
    landed between a name and an ID and split `DOE^JANE MRN0012345`'s
    reading. Read apart, both are taken."""
    regions = [_region("DOE^JANE", 0, 0, w=40, h=10),
               _region("MRN0012345", 50, 0, w=40, h=10),
               _region("SE", 20, 4, w=10, h=10)]
    assert _kept(regions, ["ids"]) == (["MRN0012345"], {"ids": 1, "left": 2})
    kept = pixel_analysis._regions_of_kinds(regions, ["names", "ids"])[0]
    assert [r.text for r in kept] == ["DOE^JANE", "MRN0012345"]


def test_a_tilted_line_is_followed_and_the_line_below_kept_apart():
    """Each word of `01 JAN 1970` 4 px lower than the last, and `CHEST`
    12 px below `01`. The band grows with each word it takes, so `1970`,
    8 px below `01`, still joins it (kills review X13: a band that does
    not grow leaves `1970` to `CHEST`'s line and the year in place);
    `CHEST`, under `01`, does not."""
    regions = [_region("01", 0, 0, w=10, h=10), _region("JAN", 12, 4, w=15, h=10),
               _region("1970", 30, 8, w=20, h=10), _region("CHEST", 0, 12, w=25, h=10)]
    assert _kept(regions, ["dates"]) == (["01", "JAN", "1970"],
                                         {"dates": 3, "left": 1})


def test_the_overlap_needed_is_half_the_bands_first_word_not_the_grown_band():
    """The overlap a word needs is half the shorter of its own height and
    the band's first word's, never of the band as it has grown: the
    tilted band below is 14 px tall once `JAN` joins, and would ask the
    20 px `1970`, 6 px into it, for 7."""
    regions = [_region("01", 0, 0, w=10, h=10), _region("JAN", 12, 4, w=15, h=10),
               _region("1970", 30, 8, w=20, h=20)]
    assert [len(line) for line in pixel_analysis._lines_of(regions)] == [3]


def test_a_word_below_the_threshold_is_not_taken_by_its_kind(tmp_path, monkeypatch):
    """`min_confidence` has one meaning (Q-B17 (a)): a date read at 79 is
    no box under `[dates]`, as it is none under `any`."""
    fake = WordsTesseract((DOB,), conf=79)
    monkeypatch.setattr(pixel_analysis, "HAS_OCR", True)
    monkeypatch.setattr(pixel_analysis, "pytesseract", fake)
    with _session_with(tmp_path, (DOB,)) as session:
        inst = _instance(session)
        assert pixel_analysis._text_boxes(inst, 80, (), ["dates"]).boxes == []
        assert pixel_analysis._text_boxes(inst, 79, (), ["dates"]).boxes == [_padded(DOB)]


# --- OCR and the names model --------------------------------------------------

def test_a_narrowed_target_without_ocr_is_refused_before_anything(tmp_path, monkeypatch):
    monkeypatch.setattr(pixel_analysis, "HAS_OCR", False)
    monkeypatch.setattr(pixel_analysis, "pytesseract", None)
    db = tmp_path / "s.db"
    with _session_with(tmp_path) as session:
        session.load_config(_cfg(tmp_path, target="[dates]"))
        with pytest.raises(pixel_analysis.OcrUnavailableError) as caught:
            session.redact(show_progress=False)
        assert not isinstance(caught.value, pixel_analysis.NlpUnavailableError)
        assert _instance(session).sop_instance_uid == UID
    assert not [r for r in audit_rows(db) if r[0] == "REDACTION"]


def test_names_without_the_model_is_refused_by_redact_before_anything(tmp_path, words_ocr, no_nlp):
    """After the drain and the OCR check, before any task, UID, row or
    zone: one pass, all or nothing. The message names the extra and the
    model, and the load's own failure."""
    db = tmp_path / "s.db"
    with _session_with(tmp_path, serial="SN-1") as session:
        session.load_config(str(_write(tmp_path, (
            "privacy_profile: basic\nmachines:\n  - serial_number: SN-1\n"
            "    redaction_zones: [[60, 64, 90, 96]]\n"
            "burned_in_text:\n  action: redact\n  scope: all\n"
            "  target: [names, dates]\n"))))
        with pytest.raises(pixel_analysis.NlpUnavailableError) as caught:
            session.redact(show_progress=False)
        assert isinstance(caught.value, pixel_analysis.OcrUnavailableError)
        assert str(caught.value) == (
            "redact(): burned_in_text target names needs the nlp extra "
            '(pip install "isocenter[nlp]") and its model (python -m spacy '
            "download en_core_web_sm): ImportError: No module named 'spacy'. "
            "Nothing was read.")
        inst = _instance(session)
        assert inst.sop_instance_uid == UID
        assert "_ISOCENTER_REDACTION_HASH" not in inst.attributes
        assert services.BURNED_IN_TEXT_ATTR not in inst.attributes
    assert words_ocr.calls == 0
    assert not [r for r in audit_rows(db) if r[0] in ("REDACTION", "RISK")]


def test_dates_and_ids_need_no_model(tmp_path, words_ocr, no_nlp):
    """Red if the check fires for any narrowed target."""
    with _session_with(tmp_path) as session:
        session.load_config(_cfg(tmp_path, target="[dates, ids]"))
        assert session.redact(show_progress=False) == 1
        assert "names_by" not in _record(_instance(session))


def test_names_without_the_model_is_refused_by_the_export_read_before_the_scan(
        tmp_path, words_ocr, no_nlp, monkeypatch):
    scans = []
    monkeypatch.setattr(Session, "_scan_before_export",
                        lambda self: scans.append(1) or set())
    with _session_with(tmp_path) as session:
        session.load_config(_cfg(tmp_path, target="[names]"))
        with pytest.raises(pixel_analysis.NlpUnavailableError,
                           match=r"^export\(check_burned_in=True\): burned_in_text"):
            session.export(str(tmp_path / "out"), show_progress=False,
                           check_burned_in=True)
    assert scans == []
    assert exported_files(tmp_path / "out") == []


def test_a_policy_naming_names_loads_saves_and_scaffolds_without_the_model(tmp_path, no_nlp):
    path = _cfg(tmp_path, target="[names]")
    with Session(str(tmp_path / "s.db")) as session:
        session.load_config(path)
        session.configuration.save()
        session.create_config(str(tmp_path / "scaffold.yaml"))
        assert session.configuration.burned_in_text["target"] == ["names"]


def test_plain_export_needs_no_model_and_writes_a_clean_current_image(
        tmp_path, monkeypatch, words_ocr, nlp):
    """Owner ruling (a) on `names_by`: it lives in the record, not the
    policy hash, so a plain export on a machine with no spaCy judges a
    record current without the model, and writes an image whose current
    record holds no box. Red if `names_by` moves into the hash (the image
    would be withheld as never checked)."""
    words_ocr.words = [LABEL]
    out = tmp_path / "out"
    with _session_with(tmp_path, (LABEL,)) as session:
        session.load_config(_cfg(tmp_path, target="[names]"))
        session.redact(show_progress=False)
        assert _record(_instance(session))["boxes"] == []
        monkeypatch.setattr(text_kinds, "_MODEL", None)
        monkeypatch.setattr(text_kinds, "_load_model", _no_model)
        summary = session.export(str(out), use_compression=False,
                                 show_progress=False)
    assert summary.written == 1
    assert len(exported_files(out)) == 1


def test_a_model_upgrade_is_read_again_by_redact(tmp_path, words_ocr, nlp):
    """Owner ruling (a): a record another model read is not current, so
    `redact()` reads it again; the same model reads nothing. Red if the
    `names_by` comparison is dropped."""
    with _session_with(tmp_path) as session:
        session.load_config(_cfg(tmp_path, action="report", target="[names]"))
        session.redact(show_progress=False)
        calls = words_ocr.calls
        session.redact(show_progress=False)
        assert words_ocr.calls == calls
        nlp.meta = {"version": "3.9.0"}
        session.redact(show_progress=False)
        assert words_ocr.calls == calls + 1
        assert _record(_instance(session))["names_by"] == (
            "spacy 3.8.16 en_core_web_sm 3.9.0")


def test_a_worker_that_cannot_load_the_model_leaves_no_record(tmp_path, words_ocr, monkeypatch):
    """The caller's model loaded and a worker's did not (a spawned
    environment that differs): a failure for that instance, no record,
    and the export withholds it. The row names the load's exception class
    only, never its message, which is a third party's and differs between
    interpreters (review F4 of #1124, as #747 and #1022 quote)."""
    answers = iter([None])
    monkeypatch.setattr(text_kinds, "_nlp_load_failure",
                        lambda: next(answers, OSError("no model at /home/QZXJ")))
    monkeypatch.setattr(text_kinds, "_names_by", lambda: "spacy x en_core_web_sm y")
    db = tmp_path / "s.db"
    out = tmp_path / "out"
    with _session_with(tmp_path) as session:
        session.load_config(_cfg(tmp_path, target="[names]"))
        session.redact(show_progress=False)
        assert _record(_instance(session)) is None
        summary = session.export(str(out), use_compression=False, show_progress=False)
    assert summary.written == 0
    assert words_ocr.calls == 0
    rows = audit_rows(db)
    assert [r for r in rows
            if "the names model could not be loaded (OSError)" in r[2]]
    assert not any("QZXJ" in str(cell) for row in rows for cell in row)


# --- the residual read, the rows, section 5 ------------------------------------

def test_the_read_after_redaction_counts_only_text_of_a_named_kind(tmp_path, words_ocr, nlp):
    """The label a narrowed target left is still there after the
    redaction; the read after it must not raise it as text still found.
    Red if the residual read runs under `any`."""
    db = tmp_path / "s.db"
    with _session_with(tmp_path) as session:
        session.load_config(_cfg(tmp_path, target="[names]"))
        session.redact(show_progress=False)
    assert not [r for r in audit_rows(db) if r[0] == "WARNING"]


def test_a_second_pass_writes_no_second_risk_row(tmp_path, words_ocr):
    db = tmp_path / "s.db"
    with _session_with(tmp_path) as session:
        session.load_config(_cfg(tmp_path, target="[ids]"))
        session.redact(show_progress=False)
        session.redact(show_progress=False)
    assert len([r for r in audit_rows(db) if r[0] == "RISK"]) == 1


def test_the_pass_row_and_section_5_say_the_target_and_its_counts(tmp_path, words_ocr, nlp):
    db = tmp_path / "s.db"
    with _session_with(tmp_path) as session:
        session.load_config(_cfg(tmp_path, target="[names, dates]"))
        session.redact(show_progress=False)
        session.generate_report(str(tmp_path / "r.md"))
    [row] = [r for r in audit_rows(db) if r[0] == "REDACTION"]
    assert row[2] == (
        "burned_in_text (action redact, scope all, target [names, dates], "
        "min_confidence 80): read 1 of 1 in-scope instance(s) holding pixels "
        "(0 out of scope), 0 could not be read, 0 already checked under this "
        "policy; text found on 1 (3 region(s)); 1 redacted; 2 region(s) read "
        "as names, 1 as dates, 2 left in place. OCR by tesseract 5.5.3; names "
        "by spacy 3.8.16 en_core_web_sm 3.8.0.")
    report = (tmp_path / "r.md").read_text()
    assert ("redact() under action redact and scope all and target [names, "
            "dates] read 1 of 1 in-scope instance(s) holding pixels (0 already "
            "checked, 0 could not be read; 0 out of scope, not read) and found "
            "text on 1 (3 region(s)); 1 redacted; 2 region(s) read as names, 1 "
            "as dates, 2 left in place.") in report
    # A RISK row does not grade.
    assert "| **Validation Status** | **PASS** |" in report


def test_a_hand_built_summary_without_the_target_fields_renders_as_before():
    summary = BurnedInTextSummary(source="redact()", action="report",
                                  attempted=1, read=1, unread=0, skipped=0,
                                  with_text=0, regions=0, acted=0, scope="all")
    assert summary.target == "" and summary.kinds == ""
    report = ComplianceReport.__new__(ComplianceReport)
    report.burned_in_text_passes = [summary]
    from isocenter import reporting
    assert "target" not in reporting._burned_in_text_line(report)


def test_check_burned_in_after_anonymize_withholds_the_image_whose_name_it_reads(
        tmp_path, words_ocr, nlp):
    """No identifier is needed: the read classifies kinds of text, so it
    is not blind after `anonymize()` replaced the name in the tags."""
    db = tmp_path / "s.db"
    out = tmp_path / "out"
    with _session_with(tmp_path) as session:
        session.load_config(_cfg(tmp_path, target="[names]"))
        session.anonymize()
        summary = session.export(str(out), use_compression=False,
                                 show_progress=False, check_burned_in=True)
        session.generate_report(str(tmp_path / "r.md"))
    assert summary.written == 0
    assert [r for r in audit_rows(db) if r[0] == "WARNING"
            and "found 2 text region(s)" in r[2]]
    assert "2 region(s) read as names, 3 left in place" in (tmp_path / "r.md").read_text()


def test_check_burned_in_writes_an_image_whose_text_is_none_of_its_kinds(
        tmp_path, words_ocr, nlp):
    out = tmp_path / "out"
    words_ocr.words = [LABEL]
    with _session_with(tmp_path, (LABEL,)) as session:
        session.load_config(_cfg(tmp_path, target="[names, dates, ids]"))
        session.anonymize()
        summary = session.export(str(out), use_compression=False,
                                 show_progress=False, check_burned_in=True)
    assert summary.written == 1


# --- nothing read is carried anywhere (§4.7) ------------------------------------

S_NAME = ("QZXJNAMESENTINEL", 2, 4, 40, 8, 1)
S_DATE = ("29/02/1888", 46, 4, 30, 8, 1)
S_ID = ("QZ987654321", 2, 30, 40, 8, 2)
S_LABEL = ("QZXJLABELSENTINEL", 46, 30, 40, 8, 2)
SENTINELS = [S_NAME[0], S_DATE[0], S_ID[0], S_LABEL[0]]


def _sentinel_nlp(monkeypatch):
    fake = FakeNlp({f"{S_NAME[0]} {S_DATE[0]}": [("PERSON", S_NAME[0])]})
    monkeypatch.setattr(text_kinds, "_MODEL", None)
    monkeypatch.setattr(text_kinds, "_load_model", lambda: fake)
    monkeypatch.setattr(text_kinds, "_spacy_version", lambda: "3.8.16")


def _sweep(tmp_path, db, caplog, caught, extra=()):
    texts = [repr(o) for o in extra]
    texts += [str(cell) for row in audit_rows(db) for cell in row]
    texts.append((tmp_path / "r.md").read_text())
    texts.append(caplog.text)
    texts += [str(w.message) for w in caught]
    with sqlite3.connect(str(db)) as conn:
        texts.append("\n".join(conn.iterdump()))
    for sentinel in SENTINELS:
        assert not any(sentinel in text for text in texts), sentinel


@pytest.mark.parametrize("target", ["[names, dates, ids]", "[dates]"])
def test_nothing_read_reaches_a_row_log_warning_report_store_or_outcome(
        tmp_path, monkeypatch, caplog, target):
    caplog.set_level(logging.DEBUG)
    _sentinel_nlp(monkeypatch)
    words = (S_NAME, S_DATE, S_ID, S_LABEL)
    fake = WordsTesseract(words)
    monkeypatch.setattr(pixel_analysis, "HAS_OCR", True)
    monkeypatch.setattr(pixel_analysis, "pytesseract", fake)
    db = tmp_path / "s.db"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with _session_with(tmp_path, words) as session:
            session.load_config(_cfg(tmp_path, target=target))
            session.redact(show_progress=False)
            outcome = session_module._burned_in_text_worker(
                (_instance(session), (), 80, None, None, ["dates", "ids", "names"]))
            session.export(str(tmp_path / "out"), use_compression=False,
                           show_progress=False)
            session.export(str(tmp_path / "out2"), use_compression=False,
                           show_progress=False, check_burned_in=True)
            session.generate_report(str(tmp_path / "r.md"))
            session.save(sync=True)
            record = _record(_instance(session))
    _sweep(tmp_path, db, caplog, caught, extra=(outcome, record))
    assert "text" not in type(outcome)._fields
    assert fake.calls > 0


def test_a_classifier_that_raises_quotes_no_text(tmp_path, monkeypatch, caplog):
    """An exception raised while the kinds were read reaches a row as its
    class only; the instance gets no record and the export withholds it."""
    caplog.set_level(logging.DEBUG)
    words = (S_NAME, S_DATE, S_ID)
    fake = WordsTesseract(words)
    monkeypatch.setattr(pixel_analysis, "HAS_OCR", True)
    monkeypatch.setattr(pixel_analysis, "pytesseract", fake)

    def boom(line, kinds):
        raise ValueError(f"could not read {line}")
    monkeypatch.setattr(text_kinds, "spans", boom)
    db = tmp_path / "s.db"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with _session_with(tmp_path, words) as session:
            session.load_config(_cfg(tmp_path, target="[dates]"))
            session.redact(show_progress=False)
            assert _record(_instance(session)) is None
            summary = session.export(str(tmp_path / "out"), use_compression=False,
                                     show_progress=False)
            session.export(str(tmp_path / "out2"), use_compression=False,
                           show_progress=False, check_burned_in=True)
            session.generate_report(str(tmp_path / "r.md"))
    assert summary.written == 0
    assert [r for r in audit_rows(db) if "the read raised ValueError" in r[2]]
    _sweep(tmp_path, db, caplog, caught)

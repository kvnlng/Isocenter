"""`SCRUB` target `orgs`, and burned-in `names`, with the real spaCy model
(#1130, owner rulings R15, R17 and R18 of 2026-10-16).

The model is deliberately not pinned (R18): CI installs whichever
`en_core_web_sm` `python -m spacy download` picks for the installed spaCy.
So nothing here pins a figure a model bump could move. Each test asserts
a property that holds whatever spans a model returns:

- `orgs` never takes away what `dates` and `ids` remove, because those
  are regular expressions and SCRUB takes the union;
- SCRUB only deletes characters;
- a multi-word institution ending in `Hospital`, `Clinic` or `Center`,
  the most canonical `ORG` shape, goes with its connector;
- the burned-in `names` kind finds a mixed-case given-plus-family name.

**Skipped where the model cannot load**, naming the load's failure: the
`nlp` extra and its model are optional, and the free-threaded build can
never install them (spaCy and thinc publish no free-threaded wheel).
**Except** under `ISOCENTER_REQUIRE_NLP_MODEL=1`, which `tests.yml` sets
on every job that is not free-threaded: there the skip is a failure
(#44's shape, as `ISOCENTER_REQUIRE_TEXT_DETECTOR` does for OpenCV).

The institution names are invented, but for `Harris Community Clinic`,
which #1130 quotes.
"""
import os
from pathlib import Path

import pytest

from isocenter import text_kinds
from isocenter.text_scrub import scrub

from support.scrub_corpus import CLINICAL, ISSUE_SOURCES

REQUIRED = os.environ.get("ISOCENTER_REQUIRE_NLP_MODEL") == "1"
CORPUS = CLINICAL + ISSUE_SOURCES


def _unavailable():
    return text_kinds._nlp_unavailable_reason()  # pylint: disable=protected-access


def test_the_model_is_installed_where_ci_requires_it():
    """Fails, never skips, when the variable says the model must load."""
    if not REQUIRED:
        pytest.skip("ISOCENTER_REQUIRE_NLP_MODEL is not 1: the 'nlp' extra "
                    "and en_core_web_sm are optional here")
    assert _unavailable() is None, (
        "ISOCENTER_REQUIRE_NLP_MODEL=1 and the spaCy model cannot load: "
        f"{_unavailable()}. Install the 'nlp' extra and run "
        "python -m spacy download en_core_web_sm.")
    # Which model, with any version (R18: no version is asserted).
    assert " en_core_web_sm " in text_kinds._names_by()  # pylint: disable=protected-access


@pytest.fixture(autouse=True)
def _needs_the_model(request):
    if request.node.name in ("test_the_model_is_installed_where_ci_requires_it",
                             "test_the_variable_is_in_the_registry"):
        return
    reason = _unavailable()
    if reason is None:
        return
    if REQUIRED:
        pytest.fail(f"ISOCENTER_REQUIRE_NLP_MODEL=1 and the spaCy model "
                    f"cannot load: {reason}")
    pytest.skip("Requires the 'nlp' extra and en_core_web_sm, which have no "
                f"free-threaded wheel: {reason}")


def test_the_variable_is_in_the_registry():
    """Read by the suite only, and registered beside the text detector's.
    Needs no model, so it never skips."""
    registry = (Path(__file__).resolve().parents[1] / "docs" / "environment.md"
                ).read_text(encoding="utf-8")
    assert "| **`ISOCENTER_REQUIRE_NLP_MODEL`** |" in registry


def _regex_spans(text):
    return (text_kinds.date_spans(text, ocr=False)
            + text_kinds.id_spans(text, ocr=False))


def test_orgs_never_changes_what_dates_and_ids_remove():
    """Every date and id `[dates, ids]` removes is gone under `[dates,
    ids, orgs]` too: a model can add removals, never take one away. One
    test over the corpus, so the skip count is one test (RELEASING.md)."""
    for text in CORPUS:
        with_orgs, _ = scrub(text, ["dates", "ids", "orgs"])
        without, _ = scrub(text, ["dates", "ids"])
        for start, end in _regex_spans(text):
            assert text[start:end] not in without
            assert text[start:end] not in with_orgs, (text, with_orgs)


def _is_subsequence(short, long):
    letters = iter(long)
    return all(ch in letters for ch in short)


def test_scrub_with_the_real_model_only_deletes_and_settles_in_one_pass():
    """The design's invariant whatever spans a model returns (owner ruling
    Q10 (a)): the result is the source with characters deleted. And a
    second pass over the written value changes nothing and finds nothing,
    the dummy `ANONYMIZED` included, so a second `audit()` and
    `anonymize()` leave a scrubbed value as the first wrote it: `scrub`
    repeats to its own fixpoint within one pass, so whatever `orgs` takes
    away, it takes away the first time. One test, so the skip count does
    not move (RELEASING.md)."""
    kinds, names = ["names", "dates", "ids", "orgs"], ("MARSH", "ELENA")
    for text in CORPUS:
        cleaned, counts = scrub(text, kinds, names)
        assert len(cleaned) <= len(text)
        assert _is_subsequence(cleaned, text), (text, cleaned)
        assert all(isinstance(n, int) and n > 0 for n in counts.values())
        written = cleaned or "ANONYMIZED"
        assert scrub(written, kinds, names) == (written, {}), (text, written)


HOSPITALS = [
    ("CT HEAD at Northwind General Hospital", "Northwind General Hospital"),
    ("LUNG CA ACCRIN 6668 at Harris Community Clinic", "Harris Community Clinic"),
    ("MR BRAIN at Lakeshore Regional Medical Center",
     "Lakeshore Regional Medical Center"),
]


def _words_gone(cleaned, name):
    words = cleaned.upper().split()
    assert not any(word.upper() in words for word in name.split()), cleaned
    assert "AT" not in words, cleaned


def test_orgs_removes_a_named_hospital(tmp_path, monkeypatch):
    """Deliberately not asserted: that the clinical half survives, which
    is what moves between model versions.

    And once through a session whose audit runs in worker processes
    (`ISOCENTER_FORCE_PROCESSES=1`), the path a default `audit()` takes on
    a GIL build: each spawned worker loads the model for itself, which no
    stand-in-model test reaches, since those patch the parent's loader and
    force threads. In this test rather than one of its own, so the skip
    count stays one test per property (RELEASING.md)."""
    for text, name in HOSPITALS:
        cleaned, counts = scrub(text, ["orgs"])
        _words_gone(cleaned, name)
        assert counts.get("orgs", 0) >= 1

    import pydicom  # pylint: disable=import-outside-toplevel
    from isocenter import Session  # pylint: disable=import-outside-toplevel
    from support.ct_small_files import write_ct  # pylint: disable=import-outside-toplevel
    monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_THREADS", raising=False)
    # The parent's own reader refuses: a spawned worker imports its own
    # `text_kinds`, so the hospital goes only if the scan ran in another
    # process. Run in this process (threads), the audit fails here.
    def not_in_this_process(*_args, **_kwargs):
        raise AssertionError("orgs was read in the parent, not in a worker")
    monkeypatch.setitem(text_kinds._FINDERS, "orgs", not_in_this_process)  # pylint: disable=protected-access
    text, name = HOSPITALS[0]
    path = write_ct(tmp_path / "in" / "a.dcm", "PORG", 1130)
    ds = pydicom.dcmread(path)
    ds.SeriesDescription = text
    ds.save_as(path, enforce_file_format=True)
    (tmp_path / "cfg.yaml").write_text(
        "privacy_profile: basic\nphi_tags:\n"
        "  0008,103e: {action: SCRUB, target: [orgs]}\n")
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        session.load_config(str(tmp_path / "cfg.yaml"))
        session.anonymize(session.audit())
        [patient] = session.store.patients
        written = patient.studies[0].series[0].instances[0].attributes["0008,103e"]
    _words_gone(written, name)


def test_burned_in_names_reads_a_full_name_with_the_real_model():
    """#1121's burned-in `names` (spaCy `PERSON`), not SCRUB's PN-token
    `names`. Not `SMITH^JOHN`: the `^` rule is a regular expression and
    would pass with no model."""
    for line, name in [("Report for John Smith", "John Smith"),
                       ("Seen by Dr. Mary Johnson", "Mary Johnson")]:
        start = line.index(name)
        assert (start, start + len(name)) in text_kinds.name_spans(line), line

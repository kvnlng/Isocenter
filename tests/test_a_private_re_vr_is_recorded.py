"""A private element written under a VR other than its recorded one leaves a
row (#571).

`_merge` writes a private element under the VR recorded for it at ingest
when the value still fits (#154), and otherwise through
`_fallback_encoding` -- LO, UT or UN. Measured at c8b4f15 and again at
220a20f, 3.12.14, with private tags recorded DA, US, DS, TM, LO and CS:

- under JPEG 2000 (Explicit VR on the wire), DA/US/DS `ANONYMIZED` were
  written **LO**, a 70-character LO was written **UT**, and an LO VM 2
  with an over-long value collapsed to **one UT value** -- with no row,
  and the collapse logged only by the worker, which under processes has
  no handler (#126);
- a TM `ANONYMIZED` stayed **TM**, invalid, with a pydicom `UserWarning`:
  `_value_fits_vr` checked DA/DT/TM by length only, so the fallback never
  fired. The issue's "exported as LO ... the file is valid" was false for
  TM, DT and UI;
- under the default native export (Implicit VR) no VR is on the wire, so
  a text-to-text re-VR is byte-identical -- but a re-ingest of an
  explicit-VR file records the new VR, which is permanent.

Now `_value_fits_vr` also asks pydicom's `validate_value` for DA, DT, TM,
UI and AS, so those fall back too; and the worker gathers every re-VR on
the instance -- nested ones included -- into **one** `WARNING` sentence per
instance, under any syntax, naming each tag and both VRs and **never a
value**, which after a REPLACE of user text or before it is
patient-derived. The collapse joins the same sentence and leaves the
worker's log.

A consequence to know: a *source* private DA, TM or UI that was already
non-conformant (never replaced) -- a DA `20231345`, the ACR-NEMA time
`07:27:30` -- now also falls back and draws the row.
An empty value is conformant under all five and is untouched.
"""
import itertools
import logging
import warnings as pywarnings
from datetime import date

import numpy as np
import pydicom
import pytest
from pydicom.tag import Tag

from isocenter.entities import DicomItem, Instance, Patient, Series, Study
from isocenter.io_handlers import (DicomExporter, ExportContext,
                                   _export_instance_worker, _value_fits_vr)
from isocenter.session import DicomSession

SC = "1.2.840.10008.5.1.4.1.1.7"
_serial = itertools.count(1)

#: (tag, recorded VR, value, VR written under JPEG 2000) for values that do
#: not fit their recorded VR.
RE_VR = [
    ("0029,1013", "DA", "ANONYMIZED", "LO"),
    ("0029,1015", "US", "ANONYMIZED", "LO"),
    ("0029,1016", "DS", "not-a-number", "LO"),
    ("0029,1017", "TM", "ANONYMIZED", "LO"),
    ("0029,1018", "DT", "ANONYMIZED", "LO"),
    ("0029,1019", "UI", "ANONYMIZED", "LO"),
    ("0029,101a", "LO", "x" * 70, "UT"),
]
COLLAPSE = ("0029,101b", "LO", ["y" * 70, "z"])
FITS = ("0029,1014", "DA", "20230515")
#: Does not fit its recorded LO (an `int` is no LO value), and the fallback
#: writes it LO anyway: stringified, the same VR, so nothing to report.
SAME_VR = ("0029,101c", "LO", 5)


def _image(extra=(), vrs=()):
    inst = Instance(f"1.2.826.0.1.571.{next(_serial)}", SC, 1)
    inst.file_path = None
    for tag, value in (("0008,0020", "20230101"), ("0008,0030", "120000"),
                       ("0008,0060", "OT"), ("0028,0002", 1),
                       ("0028,0004", "MONOCHROME2"),
                       ("0029,0010", "PROBE 571")):
        inst.set_attr(tag, value)
    inst.record_attr_vr("0029,0010", "LO")
    for tag, value in extra:
        inst.set_attr(tag, value)
    for tag, vr in vrs:
        inst.record_attr_vr(tag, vr)
    inst.set_pixel_data(np.arange(64, dtype=np.uint16).reshape(8, 8))
    return inst


def _every_case():
    cases = RE_VR + [(COLLAPSE[0], COLLAPSE[1], COLLAPSE[2], "UT"),
                     (FITS[0], FITS[1], FITS[2], FITS[1]),
                     (SAME_VR[0], SAME_VR[1], SAME_VR[2], SAME_VR[1])]
    return _image(extra=[(t, v) for t, _, v, _ in cases],
                  vrs=[(t, vr) for t, vr, _, _ in cases])


def _export(tmp_path, inst, **kwargs):
    return _export_instance_worker(ExportContext(
        instance=inst,
        output_path=str(tmp_path / "out" / f"{inst.sop_instance_uid}.dcm"),
        patient_attributes={"0010,0010": "ANON", "0010,0020": "PAT1"},
        study_attributes={"0020,000d": "1.2.826.0.2.1"},
        series_attributes={"0020,000e": "1.2.826.0.3.1"},
        **kwargs))


def _re_vr_sentences(outcome):
    return [w for w in outcome.warnings if "Private element" in w]


def _no_value_text(sentence):
    for _, _, value, _ in RE_VR:
        assert value not in sentence, sentence
    for value in COLLAPSE[2]:
        assert value not in sentence, sentence


# ---------------------------------------------------------------------------
# One sentence per instance, any syntax, no values
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("compression", [None, "j2k"])
def test_a_re_vr_writes_one_warning_naming_each_tag(tmp_path, compression):
    """Every re-VR on the instance in one sentence: each tag with its
    recorded and written VR, the collapse with its multiplicity, the
    fitting tag and the one the fallback writes under its recorded VR
    absent, and no value text. Under JPEG 2000 the file's own VRs agree
    with the sentence. Killing mutations: `revrs` not passed from the
    worker (no sentence); the sentence built only for an explicit syntax
    (the native case is silent); a record for every recorded VR (the
    fitting DA is named); a record for every fallback, whatever VR it
    writes (the int under LO is named)."""
    outcome = _export(tmp_path, _every_case(), compression=compression)

    assert outcome.ok, outcome.error
    sentences = _re_vr_sentences(outcome)
    assert len(sentences) == 1, outcome.warnings
    sentence = sentences[0]
    for tag, recorded, _, written in RE_VR:
        assert f"({tag}) recorded {recorded}, written {written}" in sentence, \
            sentence
    assert f"({COLLAPSE[0]}) recorded LO VM 2, written as one UT value" \
        in sentence, sentence
    assert f"({FITS[0]})" not in sentence, sentence
    assert f"({SAME_VR[0]})" not in sentence, sentence
    _no_value_text(sentence)

    if compression == "j2k":
        written = pydicom.dcmread(outcome.output_path)
        for tag, _, _, vr in RE_VR + [(COLLAPSE[0], None, None, "UT"),
                                      (FITS[0], None, None, "DA")]:
            group, element = (int(x, 16) for x in tag.split(","))
            assert written[Tag(group, element)].VR == vr, tag


def test_a_fitting_private_value_writes_no_sentence(tmp_path):
    """The control: recorded DA `20230515` keeps its VR and says nothing.
    Killing mutation: a record for every recorded VR."""
    inst = _image(extra=[FITS[:1] + FITS[2:]], vrs=[FITS[:2]])

    outcome = _export(tmp_path, inst, compression="j2k")

    assert outcome.ok, outcome.error
    assert _re_vr_sentences(outcome) == [], outcome.warnings


def test_a_nested_re_vr_shares_the_instance_sentence(tmp_path):
    """A private element inside a sequence item is named in the same one
    sentence, with the sequence it sits in. Killing mutations: `revrs` not
    threaded through `_merge_sequences` (the nested tag is missing); a
    sentence per merge (two sentences)."""
    item = DicomItem()
    item.set_attr("0029,0010", "PROBE 571")
    item.record_attr_vr("0029,0010", "LO")
    item.set_attr("0029,1013", "ANONYMIZED")
    item.record_attr_vr("0029,1013", "DA")
    inst = _image(extra=[("0029,1015", "ANONYMIZED")],
                  vrs=[("0029,1015", "US")])
    inst.add_sequence_item("0008,1140", item)

    outcome = _export(tmp_path, inst, compression="j2k")

    assert outcome.ok, outcome.error
    sentences = _re_vr_sentences(outcome)
    assert len(sentences) == 1, outcome.warnings
    assert "(0029,1015) recorded US, written LO" in sentences[0], sentences
    assert "(0029,1013) in (0008,1140) recorded DA, written LO" \
        in sentences[0], sentences
    written = pydicom.dcmread(outcome.output_path)
    assert written.ReferencedImageSequence[0][Tag(0x0029, 0x1013)].VR == "LO"


def test_the_sentence_names_ten_tags_and_counts_the_rest(tmp_path):
    """Twelve re-VR'd tags: ten named, then "and 2 more". Killing
    mutations: the cap removed; the count of the rest wrong."""
    tags = [f"0029,10{n:02x}" for n in range(0x20, 0x2c)]
    inst = _image(extra=[(t, "ANONYMIZED") for t in tags],
                  vrs=[(t, "DA") for t in tags])

    outcome = _export(tmp_path, inst)

    sentences = _re_vr_sentences(outcome)
    assert len(sentences) == 1, outcome.warnings
    named = [t for t in tags if f"({t})" in sentences[0]]
    assert named == tags[:10], sentences
    assert "and 2 more" in sentences[0], sentences


# ---------------------------------------------------------------------------
# The fit check, and the collapse's channel
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("vr, value", [
    ("DA", "ANONYMIZ"), ("TM", "ANONYMIZED"), ("DT", "ANONYMIZED"),
    ("UI", "ANONYMIZED"), ("AS", "ANON")])
def test_a_value_the_vr_cannot_hold_does_not_fit(vr, value):
    """`validate_value` for the five format-checked text VRs. Each value
    is within its VR's length cap, so the cap alone passed every one.
    Killing mutations: the check removed; a VR dropped from its set."""
    assert not _value_fits_vr(value, vr)


@pytest.mark.parametrize("vr, value", [
    ("DA", "20230515"), ("DA", ""),
    ("TM", "072730.123456"), ("TM", ""), ("DT", "20230515072730"),
    ("UI", "1.2.840.10008.1.2"), ("UI", ""), ("AS", "045Y"), ("AS", "")])
def test_a_conformant_or_empty_value_still_fits(vr, value):
    """The control: a conformant value and the empty value each still fit.
    (A DA range, `20230101-20231231`, passes `validate_value` -- it is the
    query form -- and was already refused by DA's 8-character cap, which
    stays.) Killing mutation: the check applied too widely (empty
    refused)."""
    assert _value_fits_vr(value, vr)


def test_tm_dt_ui_that_do_not_fit_fall_back_without_a_pydicom_warning(
        tmp_path):
    """Under JPEG 2000 the three that used to keep an invalid value are
    written LO, and pydicom has nothing to warn about. Killing mutation:
    `validate_value` removed from `_value_fits_vr` (TM stays TM, with a
    `UserWarning`)."""
    cases = [c for c in RE_VR if c[1] in ("TM", "DT", "UI")]
    inst = _image(extra=[(t, v) for t, _, v, _ in cases],
                  vrs=[(t, vr) for t, vr, _, _ in cases])

    with pywarnings.catch_warnings(record=True) as caught:
        pywarnings.simplefilter("always")
        outcome = _export(tmp_path, inst, compression="j2k")

    assert outcome.ok, outcome.error
    assert not [w for w in caught if "Invalid value for VR" in str(w.message)]
    written = pydicom.dcmread(outcome.output_path)
    for tag, _, _, _ in cases:
        group, element = (int(x, 16) for x in tag.split(","))
        assert written[Tag(group, element)].VR == "LO", tag


def test_the_collapse_is_not_logged_in_the_worker(tmp_path, caplog):
    """The collapse rides `outcome.warnings`, which the parent logs and
    audits; the worker logs nothing of its own. Recorded or not: an
    element from an Implicit VR source has no recorded VR, and its VM n ->
    one UT value is a shape change all the same. Killing mutations: the
    worker-side log restored; the collapse reported only when a VR was
    recorded (the unrecorded one is silent)."""
    recorded = _image(extra=[(COLLAPSE[0], COLLAPSE[2])], vrs=[COLLAPSE[:2]])
    unrecorded = _image(extra=[(COLLAPSE[0], COLLAPSE[2])])

    with caplog.at_level(logging.WARNING):
        outcomes = [_export(tmp_path, recorded), _export(tmp_path, unrecorded)]

    assert all(o.ok for o in outcomes), [o.error for o in outcomes]
    assert [r for r in caplog.records
            if COLLAPSE[0] in r.getMessage()] == [], caplog.records
    sentences = [_re_vr_sentences(o) for o in outcomes]
    assert [len(s) for s in sentences] == [1, 1], sentences
    assert f"({COLLAPSE[0]}) recorded LO VM 2, written as one UT value" \
        in sentences[0][0], sentences
    assert f"({COLLAPSE[0]}) VM 2, written as one UT value" \
        in sentences[1][0], sentences


def test_merge_without_an_accumulator_still_says_so(caplog):
    """A direct `_merge` call with nowhere to put the collapse logs it, as
    `losses=None` does -- the unit-level contract
    `test_the_overlong_collapse_names_the_tag_and_the_arity_change` pins."""
    ds = pydicom.Dataset()
    with caplog.at_level(logging.WARNING):
        DicomExporter._merge(ds, {"0009,1007": ["x" * 80, "y"]})
    assert any("0009,1007" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# The parent's half: one row per instance, and the grade
# ---------------------------------------------------------------------------

_LEVERS = ("ISOCENTER_FORCE_THREADS", "ISOCENTER_FORCE_PROCESSES",
           "ISOCENTER_MAX_TASKS_PER_CHILD")


@pytest.mark.parametrize("threads", [False, True])
def test_one_row_per_instance_reaches_the_audit_log_and_the_grade(
        tmp_path, monkeypatch, threads):
    """Two instances each carrying re-VRs, one clean: two `WARNING` rows,
    none carrying a value, and the run grades `REVIEW_REQUIRED`. Killing
    mutation: the sentence not appended to `warnings` (no row, PASS)."""
    for name in _LEVERS:
        monkeypatch.delenv(name, raising=False)
    if threads:
        monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    patient = Patient("PAT571", "ANON")
    study = Study("1.2.826.0.2.571", date(2023, 1, 1))
    series = Series("1.2.826.0.3.571", "OT", 1)
    series.instances.extend([_every_case(), _every_case(), _image()])
    study.series.append(series)
    patient.studies.append(study)
    report = tmp_path / "report.md"

    with DicomSession(str(tmp_path / "s.db")) as session:
        session.store.patients.append(patient)
        session.save()
        session.export(str(tmp_path / "out"), use_compression=False,
                       show_progress=False)
        rows = [tuple(r) for r in session.store_backend.get_audit_errors()]
        session.generate_report(str(report))

    re_vr = [r for r in rows if "Private element" in r[2]]
    assert len(re_vr) == 2, rows
    assert {r[1] for r in re_vr} == {"WARNING"}
    for row in re_vr:
        _no_value_text(row[2])
    grade = [line for line in report.read_text().splitlines()
             if "Grade Basis" in line]
    assert grade and "REVIEW_REQUIRED" in grade[0], grade

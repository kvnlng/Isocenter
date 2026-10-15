"""`export(check_burned_in=True)` under a `burned_in_text` policy reads the
pixels it is about to write (#1108, owner ruling Q6 of 2026-10-14).

Plain `export()` reads each instance's record of the last `redact()` pass
and never a pixel (`test_redact_blanks_burned_in_text.py`). With
`check_burned_in=True` and the key configured, the export also runs the
pass's OCR read over every planned instance that holds pixels, as it is
about to be written (the configured zones zeroed on a copy first, as the
export zeroes them), and that fresh read decides the instance, not the
record:

- text found: written with a `WARNING` row under `report`; withheld with
  a row under `redact` and `withhold`. The export never redacts: it does
  not change the graph, the store or a UID, and writes no record; under
  `redact` the row names the remedy, `redact()`, or `redact(force=True)`
  for an instance `redact()` has already read;
- nothing found: written, whatever the record says, and no row;
- not read in full: withheld under `redact`/`withhold`, written under
  `report`, with a row: a frame not read is a frame not checked;
- OCR unavailable: `OcrUnavailableError` before the pre-export scan, the
  save, any row or any file.

Without the key, `check_burned_in=True` is the tag scan alone, as before.
The OCR stand-in and the image writer are the redact tests'; threads only.
"""
import pydicom
import pytest

from isocenter import entities, pixel_analysis, services
from isocenter.session import DicomSession as Session

from test_redact_blanks_burned_in_text import (
    INK, INK_BOX, ONE, PADDED, PASS_LINE, REVIEW_LINE, TWO, FakeTesseract,
    _cohort, _config, audit_rows,
    exported_files, pixels_of, write_sr)


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


@pytest.fixture
def ocr(monkeypatch):
    fake = FakeTesseract()
    monkeypatch.setattr(pixel_analysis, "HAS_OCR", True)
    monkeypatch.setattr(pixel_analysis, "pytesseract", fake)
    return fake


def _instances(session):
    return {inst.attributes.get(entities.SOURCE_SOP_UID_ATTR,
                                inst.sop_instance_uid): inst
            for p in session.store.patients for st in p.studies
            for se in st.series for inst in se.instances}


def _ready(tmp_path, action, *images):
    """A session with the cohort ingested, the policy loaded and the tags
    anonymized, so the pre-export tag scan withholds nothing."""
    session = Session(str(tmp_path / "s.db"))
    session.ingest(_cohort(tmp_path, *images))
    session.load_config(_config(tmp_path, action))
    session.anonymize()
    return session


REMEDY = ("; run redact(), or redact(force=True) if redact() has already "
          "read it under this policy")


def _found(out, verb, uid, action):
    remedy = REMEDY if action == "redact" else ""
    return (f"DICOM export to {out} {verb} instance {uid}: the pre-export read "
            f"(check_burned_in=True) found 1 text region(s) on frame(s) 0 "
            f"(action: {action}){remedy}.")


@pytest.mark.parametrize("action, written", [("report", True), ("redact", False),
                                             ("withhold", False)])
def test_text_found_by_the_read_decides_the_instance(tmp_path, ocr, action, written):
    """No `redact()`: the record gate alone would say "not checked" for
    both; the read writes the clean one with no row, and acts on the
    other by the action. Kills the read not run, the record gate left in
    charge, and a found box ignored."""
    out = tmp_path / "out"
    with _ready(tmp_path, action, (ONE, {"ink": [(0, *INK_BOX)]}),
                (TWO, {"series_uid": "1.2.826.0.1.3680043.8.498.1108.9"})) as session:
        live = _instances(session)
        inked, clean = live[ONE].sop_instance_uid, live[TWO].sop_instance_uid
        summary = session.export(str(out), use_compression=False,
                                 show_progress=False, check_burned_in=True)
        db = session.store_backend.db_path
    assert clean in summary.written_uids
    assert (inked in summary.written_uids) is written
    gate = [r for r in audit_rows(db) if r[0] == "WARNING"]
    assert gate == [("WARNING", inked,
                     _found(out, "did not withhold" if written else "withheld", inked, action))]
    [export_row] = [r for r in audit_rows(db) if r[0] == "EXPORT"]
    planned = 2 if written else 1
    tail = "" if written else "; 1 more withheld by the burned_in_text policy"
    assert export_row[2] == (f"DICOM export to {out}: wrote {planned} of {planned} "
                             f"planned instances from 1 patients{tail}.")


@pytest.mark.parametrize("action", ["report", "redact", "withhold"])
def test_the_export_changes_nothing_it_read(tmp_path, ocr, action):
    """The read is a check: no record, no pixel, no UID moves in the graph,
    and a `report` export writes the ink as found."""
    out = tmp_path / "out"
    with _ready(tmp_path, action, (ONE, {"ink": [(0, *INK_BOX)]})) as session:
        inst = _instances(session)[ONE]
        uid, before = inst.sop_instance_uid, inst.get_pixel_data().copy()
        session.export(str(out), use_compression=False, show_progress=False,
                       check_burned_in=True)
        assert services.BURNED_IN_TEXT_ATTR not in inst.attributes
        assert "_ISOCENTER_REDACTION_HASH" not in inst.attributes
        assert inst.sop_instance_uid == uid
        assert (inst.get_pixel_data() == before).all()
    if action == "report":
        [path] = exported_files(out)
        assert (pixels_of(path) == INK).any()
    else:
        assert exported_files(out) == []


def test_after_redact_the_read_finds_nothing_and_writes_it(tmp_path, ocr):
    """The read is of what is exported: after `redact()` blanked the text,
    nothing is found, so no row; the instance reads once in the pass,
    once in its residual read, and once at the export."""
    out = tmp_path / "out"
    with _ready(tmp_path, "redact", (ONE, {"ink": [(0, *INK_BOX)]})) as session:
        assert session.redact(show_progress=False) == 1
        calls = ocr.calls
        summary = session.export(str(out), use_compression=False,
                                 show_progress=False, check_burned_in=True)
        db = session.store_backend.db_path
    assert ocr.calls == calls + 1
    assert summary.written == 1
    assert not [r for r in audit_rows(db) if r[0] == "WARNING"]
    [path] = exported_files(out)
    assert (pixels_of(path) != INK).all()


def test_a_record_with_text_is_overruled_by_a_read_that_finds_none(tmp_path, ocr):
    """`withhold` with a record naming a box, and the ink gone from the
    graph since (written in by hand): the fresh read finds nothing, so the
    instance is written. Kills the record consulted under the read."""
    out = tmp_path / "out"
    with _ready(tmp_path, "withhold", (ONE, {"ink": [(0, *INK_BOX)]})) as session:
        session.redact(show_progress=False)
        inst = _instances(session)[ONE]
        frame = inst.get_pixel_data().copy()
        frame[frame == INK] = 0
        inst.set_pixel_data(frame)
        assert services.burned_in_text_record(inst)["boxes"]
        summary = session.export(str(out), use_compression=False,
                                 show_progress=False, check_burned_in=True)
    assert summary.written == 1


@pytest.mark.parametrize("action, written", [("report", 1), ("redact", 0),
                                             ("withhold", 0)])
def test_an_instance_the_read_cannot_read_is_unchecked(tmp_path, ocr, action, written):
    out = tmp_path / "out"
    with _ready(tmp_path, action, (ONE, {"ink": [(0, *INK_BOX)]})) as session:
        uid = _instances(session)[ONE].sop_instance_uid
        ocr.fail = True
        summary = session.export(str(out), use_compression=False,
                                 show_progress=False, check_burned_in=True)
        session.generate_report(str(tmp_path / "r.md"))
        db = session.store_backend.db_path
    assert summary.written == written
    # Section 5 counts the read that failed as not read (review of #1110:
    # `unread=0` and `read=len(reads)` survived without this).
    done = "written" if written else "withheld"
    assert (f"export(check_burned_in=True) under action {action} and scope "
            f"all read 0 of 1 planned in-scope instance(s) holding pixels (1 "
            f"could not be read; 0 out of scope, not read) and found text on 0 "
            f"(0 region(s)); 1 {done}, with a row."
            ) in _section_5_line((tmp_path / "r.md").read_text())
    verb = "did not withhold" if written else "withheld"
    assert [r for r in audit_rows(db) if r[0] == "WARNING"] == [(
        "WARNING", uid,
        f"DICOM export to {out} {verb} instance {uid}: the pre-export read "
        f"(check_burned_in=True) could not read its pixels in full, so its "
        f"burned-in text was not checked.")]


def test_an_instance_with_no_pixels_is_not_read(tmp_path, ocr):
    src = tmp_path / "in"
    src.mkdir()
    write_sr(src / "sr.dcm", sop_uid=TWO)
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(src))
        session.load_config(_config(tmp_path, "withhold"))
        session.anonymize()
        summary = session.export(str(tmp_path / "out"), use_compression=False,
                                 show_progress=False, check_burned_in=True)
    assert summary.written == 1
    assert ocr.calls == 0


def test_without_the_key_check_burned_in_reads_no_pixels(tmp_path, ocr):
    cfg = tmp_path / "plain.yaml"
    cfg.write_text("privacy_profile: basic\n", encoding="utf-8")
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)]})))
        session.load_config(str(cfg))
        session.anonymize()
        summary = session.export(str(tmp_path / "out"), use_compression=False,
                                 show_progress=False, check_burned_in=True)
    assert summary.written == 1
    assert ocr.calls == 0


def test_ocr_unavailable_refuses_before_the_scan_the_save_and_any_row(tmp_path, monkeypatch):
    out = tmp_path / "out"
    with _ready(tmp_path, "report", (ONE, {"ink": [(0, *INK_BOX)]})) as session:
        monkeypatch.setattr(pixel_analysis, "HAS_OCR", False)
        monkeypatch.setattr(pixel_analysis, "pytesseract", None)
        db = session.store_backend.db_path
        rows = audit_rows(db)
        with pytest.raises(pixel_analysis.OcrUnavailableError,
                           match=r"export\(check_burned_in=True\)"):
            session.export(str(out), use_compression=False, show_progress=False,
                           check_burned_in=True)
        assert audit_rows(db) == rows
        assert not out.exists()
        # The record gate needs no OCR: plain `export()` still runs.
        assert session.export(str(out), use_compression=False,
                              show_progress=False).written == 1


def test_the_refusal_comes_before_the_tag_scan_and_the_save(tmp_path, monkeypatch):
    """The session must hold something for the scan and the save to change:
    no `audit()`, so the tag scan would record a status, and one unsaved
    edit, so the save would write it. Both are as they were after the
    raise. Kills the raise moved below the scan, or below the save (review
    of #1110, M1 and M2)."""
    out = tmp_path / "out"
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)]})))
        session.load_config(_config(tmp_path, "report"))
        session.save(sync=True)
        inst = _instances(session)[ONE]
        before = inst.phi_status
        inst.set_attr("0008,1030", "EDITED")
        assert inst.has_unsaved_changes
        monkeypatch.setattr(pixel_analysis, "HAS_OCR", False)
        monkeypatch.setattr(pixel_analysis, "pytesseract", None)
        with pytest.raises(pixel_analysis.OcrUnavailableError):
            session.export(str(out), use_compression=False, show_progress=False,
                           check_burned_in=True)
        assert inst.phi_status == before, "the tag scan ran before the refusal"
        assert inst.has_unsaved_changes, "the save ran before the refusal"


def test_the_read_is_of_the_pixels_after_the_export_zones(tmp_path, ocr):
    """A machine zone covering the ink, under `withhold`, and no
    `redact()`: the export zeroes the zone, so the read of what it writes
    finds nothing and the instance is written with no row. Kills the read
    handed no zones (review of #1110, M3)."""
    zone = (f"machines:\n  - serial_number: SN-1\n    redaction_zones:\n"
            f"      - [{PADDED[0]}, {PADDED[1]}, {PADDED[2]}, {PADDED[3]}]\n")
    out = tmp_path / "out"
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)],
                                                "serial": "SN-1"})))
        session.load_config(_config(tmp_path, "withhold", zone))
        session.anonymize()
        summary = session.export(str(out), use_compression=False,
                                 show_progress=False, check_burned_in=True)
        db = session.store_backend.db_path
    assert ocr.calls == 1
    assert summary.written == 1
    assert not [r for r in audit_rows(db) if r[0] == "WARNING"]
    [path] = exported_files(out)
    assert (pixels_of(path) != INK).all()


@pytest.mark.parametrize("selector", ['serial_number: ""', "match: {manufacturer: ACME}"],
                         ids=["empty", "match"])
def test_a_serial_less_series_is_read_after_its_rules_zones(tmp_path, ocr, selector):
    """The reconciliation of #1110 and #1111: a serial-less image whose ink
    a `""` or `match:` rule's zone covers. The read sees the zone zeroed,
    as the export writes it, so nothing is found and the image is written
    with no row. Kills the read's zones looked up by serial alone."""
    zone = (f"machines:\n  - {selector}\n    redaction_zones:\n"
            f"      - [{PADDED[0]}, {PADDED[1]}, {PADDED[2]}, {PADDED[3]}]\n")
    out = tmp_path / "out"
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)]})))
        session.load_config(_config(tmp_path, "withhold", zone))
        session.anonymize()
        summary = session.export(str(out), use_compression=False,
                                 show_progress=False, check_burned_in=True)
        db = session.store_backend.db_path
    assert ocr.calls == 1
    assert summary.written == 1
    assert not [r for r in audit_rows(db) if r[0] == "WARNING"]
    [path] = exported_files(out)
    assert (pixels_of(path) != INK).all()


def test_the_read_quotes_no_text(tmp_path, ocr, caplog):
    with _ready(tmp_path, "report", (ONE, {"ink": [(0, *INK_BOX)]})) as session:
        session.export(str(tmp_path / "out"), use_compression=False,
                       show_progress=False, check_burned_in=True)
        db = session.store_backend.db_path
    assert ocr.text not in str(audit_rows(db))
    assert ocr.text not in caplog.text
    [path] = exported_files(tmp_path / "out")
    assert ocr.text.encode() not in open(path, "rb").read()
    assert ocr.text not in str(pydicom.dcmread(path))


def _section_5_line(report):
    [line] = [l for l in report.splitlines() if "Burned-in Text" in l]
    return line


def test_a_clean_read_grades_pass_and_section_5_says_it_ran(tmp_path, ocr):
    """A read that found nothing leaves a trace in the report, as a
    `redact()` pass does, and costs no PASS: no grade condition reads it
    (coordinator's ruling on the review of #1110). Kills the summary not
    recorded, and section 5 still saying no OCR checked anything."""
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(_cohort(tmp_path, (TWO, {})))
        session.load_config(_config(tmp_path, "withhold"))
        session.audit()
        session.anonymize()
        summary = session.export(str(tmp_path / "out"), use_compression=False,
                                 show_progress=False, check_burned_in=True)
        session.generate_report(str(tmp_path / "r.md"))
    assert summary.written == 1
    report = (tmp_path / "r.md").read_text()
    assert PASS_LINE in report, report
    assert ("export(check_burned_in=True) under action withhold and scope "
            "all read 1 of 1 planned in-scope instance(s) holding pixels (0 "
            "could not be read; 0 out of scope, not read) and found text on 0 "
            "(0 region(s)); 0 withheld, with a row."
            ) in _section_5_line(report)
    assert "does not say burned-in text was checked by OCR" not in report


@pytest.mark.parametrize("action, done", [("report", "written"),
                                          ("redact", "withheld")])
def test_section_5_counts_what_the_read_found_and_never_says_redacted(
        tmp_path, ocr, action, done):
    """The export redacts nothing under any action, so its line says
    withheld or written, never `redacted`."""
    with _ready(tmp_path, action, (ONE, {"ink": [(0, *INK_BOX)]}),
                (TWO, {"series_uid": "1.2.826.0.1.3680043.8.498.1108.9"})) as session:
        session.export(str(tmp_path / "out"), use_compression=False,
                       show_progress=False, check_burned_in=True)
        session.generate_report(str(tmp_path / "r.md"))
    report = (tmp_path / "r.md").read_text()
    assert REVIEW_LINE in report
    line = _section_5_line(report)
    assert (f"export(check_burned_in=True) under action {action} and scope "
            f"all read 2 of 2 planned in-scope instance(s) holding pixels (0 "
            f"could not be read; 0 out of scope, not read) and found text on 1 "
            f"(1 region(s)); 1 {done}, with a row.") in line
    assert "redacted" not in line


def test_numbered_runs_say_which_is_the_export_read(tmp_path, ocr):
    """A `redact()` pass and an export's read in one session: two numbered
    entries, and the export's is not called a pass (review of #1110)."""
    with _ready(tmp_path, "redact", (ONE, {"ink": [(0, *INK_BOX)]})) as session:
        session.redact(show_progress=False)
        session.export(str(tmp_path / "out"), use_compression=False,
                       show_progress=False, check_burned_in=True)
        session.generate_report(str(tmp_path / "r.md"))
    line = _section_5_line((tmp_path / "r.md").read_text())
    assert "ran 2 times in this session -- pass 1: redact() under action redact" in line
    assert "; export read 2: export(check_burned_in=True) under action redact" in line

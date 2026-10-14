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
  `redact` the remedy is `redact()`, which the row names;
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
    INK, INK_BOX, ONE, TWO, FakeTesseract, _cohort, _config, audit_rows,
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


def _found(out, verb, uid, action):
    return (f"DICOM export to {out} {verb} instance {uid}: the pre-export read "
            f"(check_burned_in=True) found 1 text region(s) on frame(s) 0 "
            f"(action: {action}).")


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
        db = session.store_backend.db_path
    assert summary.written == written
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

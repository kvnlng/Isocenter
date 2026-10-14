"""`redact()` under a `burned_in_text` policy: per-image OCR redaction (#1108).

Measured on f31758d3 (the architect's probe 4, both interpreters): a
serial-less CR with a name burned into its pixels exported with every ink
pixel intact, under `export()` and `export(check_burned_in=True)`, and the
report graded PASS. No configuration could reach text that moves from
image to image: a zone is per machine, and the image had no serial.

The owner's rulings of 2026-10-14, which these tests hold:

- a top-level `burned_in_text: {action, min_confidence}` (default 80);
  `redact()` is the one verb that runs it, over every instance that holds
  pixels, serial or not;
- two phases: workers OCR and return boxes only, never text; the parent
  derives the redacted UID from the boxes under the project secret, which
  never reaches a worker; the redaction workers blank each box on its own
  frame, padded by `max(2, round(0.15 * height))`; then a residual read;
- no two-character filter (initials such as `JT` are text);
- `report` writes rows, `redact` blanks, `withhold` keeps the instance out
  of the export; an instance with pixels the policy never checked is
  withheld under `redact`/`withhold` and written with a row under `report`;
- a pass that blanked text does not cost the PASS by itself; leftover
  text, unread instances and unchecked instances do (WARNING rows);
- an OCR-only redaction writes DERIVED and code 113062 and leaves
  Burned In Annotation as the source had it;
- `OcrUnavailableError` from `redact()` before anything when the key is
  present and OCR cannot run; `redact_by_machine()` does not run the pass;
- no OCR text in any row, log line, exception, report, persisted value or
  worker return.

**How OCR is faked.** `FakeTesseract` finds "text" where a uint8 frame is
255 and returns that extent, so a blanked frame has nothing left to find
and the residual read is a real read. Threads only (`ISOCENTER_FORCE_THREADS`):
`monkeypatch` does not cross a process boundary. The real binary, and
processes, are `test_burned_in_text_with_real_tesseract.py`.
"""
import json
import logging
import os
import sqlite3
from types import SimpleNamespace

import numpy as np
import pydicom
import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian

from isocenter import pixel_analysis, privacy, services
from isocenter import session as session_module
from isocenter.session import DicomSession as Session

from support.project_secret import FIXED_A, load_fixed_secret

SC_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.7"
BASIC_TEXT_SR = "1.2.840.10008.5.1.4.1.1.88.11"
ROWS, COLS = 64, 96
BACKGROUND = 40
INK = 255
#: A string no image, row or log holds unless the OCR text leaked.
SENTINEL = "QZXJ7SENTINELWORD"
STUDY = "1.2.826.0.1.3680043.8.498.1108.1"
SERIES = "1.2.826.0.1.3680043.8.498.1108.2"
#: Ink rows 20..30 (10 high), columns 30..60: padded by max(2, 1.5) = 2.
INK_BOX = (20, 30, 30, 60)
PADDED = (18, 32, 28, 62)
PASS_LINE = "| **Validation Status** | **PASS** |"
REVIEW_LINE = "| **Validation Status** | **REVIEW_REQUIRED** |"


def write_image(path, *, sop_uid, ink=(), frames=1, burned_in=None,
                patient_id="PID-1108", series_uid=SERIES, serial=None):
    """Write an 8-bit image with ink at each `(frame, y1, y2, x1, x2)`.

    8-bit, no windowing tags: `pixel_analysis` hands it to OCR unscaled,
    so the ink is still 255 when the stand-in looks.
    """
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SC_SOP_CLASS
    meta.MediaStorageSOPInstanceUID = sop_uid
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(str(path), {}, file_meta=meta, preamble=b"\0" * 128)
    ds.SOPClassUID = SC_SOP_CLASS
    ds.SOPInstanceUID = sop_uid
    ds.StudyInstanceUID = STUDY
    ds.SeriesInstanceUID = series_uid
    ds.PatientID = patient_id
    ds.PatientName = "DOE^JANE"
    ds.StudyDate = "20240102"
    ds.Modality = "OT"
    ds.Manufacturer = "ACME"
    if serial is not None:
        ds.DeviceSerialNumber = serial
    if burned_in is not None:
        ds.BurnedInAnnotation = burned_in
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.Rows, ds.Columns = ROWS, COLS
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    arr = np.full((frames, ROWS, COLS), BACKGROUND, dtype=np.uint8)
    for frame, y1, y2, x1, x2 in ink:
        arr[frame, y1:y2, x1:x2] = INK
    if frames > 1:
        ds.NumberOfFrames = frames
    ds.PixelData = (arr if frames > 1 else arr[0]).tobytes()
    ds.save_as(str(path), enforce_file_format=True)
    return arr


def write_sr(path, *, sop_uid):
    """A Basic Text SR: an instance that holds no pixel element."""
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = BASIC_TEXT_SR
    meta.MediaStorageSOPInstanceUID = sop_uid
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(str(path), {}, file_meta=meta, preamble=b"\0" * 128)
    ds.SOPClassUID = BASIC_TEXT_SR
    ds.SOPInstanceUID = sop_uid
    ds.StudyInstanceUID = STUDY
    ds.SeriesInstanceUID = "1.2.826.0.1.3680043.8.498.1108.3"
    ds.PatientID = "PID-1108"
    ds.PatientName = "DOE^JANE"
    ds.StudyDate = "20240102"
    ds.Modality = "SR"
    ds.save_as(str(path), enforce_file_format=True)


class FakeTesseract:
    """A `pytesseract` stand-in that finds ink, for threads only."""

    def __init__(self, conf=96, text=SENTINEL):
        self.conf = conf
        self.text = text
        self.calls = 0
        #: Report the last box found even once its ink is gone, as a
        #: residual read over a partly blanked line would.
        self.sticky = False
        #: Raise, as a tesseract that exits non-zero does.
        self.fail = False
        #: Raise on these calls only (1-based), so one frame of several,
        #: or the read after redaction alone, can fail.
        self.fail_calls = set()
        self.fail_message = "tesseract exited 1: Error opening data file"
        #: `OMP_THREAD_LIMIT` as each call saw it.
        self.omp_seen = []
        self._last = None
        self.Output = SimpleNamespace(DICT="dict")

    @staticmethod
    def get_tesseract_version():
        return "5.5.3"

    def image_to_data(self, img, *args, **kwargs):
        self.calls += 1
        self.omp_seen.append(os.environ.get("OMP_THREAD_LIMIT"))
        if self.fail or self.calls in self.fail_calls:
            raise RuntimeError(self.fail_message)
        arr = np.asarray(img)
        ys, xs = np.nonzero(arr == INK)
        if len(ys):
            x, y = int(xs.min()), int(ys.min())
            self._last = (x, y, int(xs.max()) - x + 1, int(ys.max()) - y + 1)
        elif not (self.sticky and self._last):
            return {"text": [], "conf": [], "left": [], "top": [],
                    "width": [], "height": []}
        x, y, w, h = self._last
        return {"text": [self.text], "conf": [self.conf], "left": [x],
                "top": [y], "width": [w], "height": [h]}


def pixels_of(path):
    """A file's pixels as `(frames, rows, cols)`."""
    arr = pydicom.dcmread(str(path)).pixel_array
    return arr if arr.ndim == 3 else arr[None]


def exported_files(folder):
    return sorted(os.path.join(root, name)
                  for root, _dirs, names in os.walk(str(folder))
                  for name in names if name.endswith(".dcm"))


def audit_rows(db):
    with sqlite3.connect(str(db)) as conn:
        return conn.execute(
            "SELECT action_type, entity_uid, details FROM audit_log "
            "ORDER BY id").fetchall()


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


def _config(tmp_path, action, extra="", name="c.yaml"):
    path = tmp_path / name
    path.write_text(f"privacy_profile: basic\nburned_in_text:\n  action: {action}\n"
                    f"{extra}", encoding="utf-8")
    return str(path)


def _cohort(tmp_path, *images):
    """Write `(sop_uid, kwargs)` images into one input folder."""
    src = tmp_path / "in"
    src.mkdir(exist_ok=True)
    for i, (uid, kwargs) in enumerate(images):
        write_image(src / f"i{i}.dcm", sop_uid=uid, **kwargs)
    return str(src)


ONE = "1.2.826.0.1.3680043.8.498.1108.10"
TWO = "1.2.826.0.1.3680043.8.498.1108.11"


def _record(instance):
    return json.loads(instance.attributes[services.BURNED_IN_TEXT_ATTR])


def _only_instance(session):
    return session.store.patients[0].studies[0].series[0].instances[0]


def _the_box(arr, frame=0, box=PADDED):
    y1, y2, x1, x2 = box
    return arr[frame, y1:y2, x1:x2]


# --- redact ---------------------------------------------------------------

def test_redact_blanks_the_text_with_no_zone_and_no_rule(tmp_path, ocr):
    """The acceptance case, with the stand-in: no machine rule, no zone,
    no serial. Kills the pass not run when `rules` is empty, the boxes not
    padded, and the export reading the source frame."""
    db = tmp_path / "s.db"
    with Session(str(db)) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)]})))
        session.load_config(_config(tmp_path, "redact"))
        assert session.configuration.rules == []
        assert session.redact(show_progress=False) == 1
        inst = _only_instance(session)
        live = inst.get_pixel_data()[None]
        assert not _the_box(live).any()
        # Only the padded box: the rest of the frame is untouched.
        outside = live.copy()
        outside[0, PADDED[0]:PADDED[1], PADDED[2]:PADDED[3]] = BACKGROUND
        assert (outside == BACKGROUND).all()
        assert _record(inst)["boxes"] == [[0, *PADDED]]
        out = tmp_path / "out"
        session.export(str(out), use_compression=False, show_progress=False)
    [path] = exported_files(out)
    assert not _the_box(pixels_of(path)).any()
    assert (pixels_of(path) != INK).all()


def test_the_redacted_uid_is_derived_from_the_boxes_under_the_secret(tmp_path, ocr):
    """`_redaction_uid_for(source, hash(ocr=(policy, boxes)))`, in the
    parent. Kills a UID drawn at random, one derived without the boxes,
    and a zone-only hash shape reused for an OCR-only redaction."""
    with Session(str(tmp_path / "s.db")) as session:
        load_fixed_secret(session)
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)]})))
        session.load_config(_config(tmp_path, "redact"))
        session.redact(show_progress=False)
        inst = _only_instance(session)
        record = _record(inst)
        expected_hash = services._redaction_config_hash(
            [], [], ocr=(record["policy"], record["boxes"]))
        assert inst.attributes["_ISOCENTER_REDACTION_HASH"] == expected_hash
        assert inst.sop_instance_uid == privacy._redaction_uid_for(
            ONE, expected_hash, FIXED_A)
        assert expected_hash != services._redaction_config_hash([""], [])


def test_a_zone_only_hash_does_not_move_when_ocr_found_nothing(tmp_path, ocr):
    """An rc18 store redacted by zones must not re-redact because the key
    was added: with no boxes the hash is the zone-only one."""
    rois = [[0, 4, 0, 4]]
    assert (services._redaction_config_hash(["SN"], rois, ocr=None)
            == services._redaction_config_hash(["SN"], rois))
    assert (services._redaction_config_hash(["SN"], rois, ocr=("p", []))
            == services._redaction_config_hash(["SN"], rois))


def test_burned_in_annotation_is_left_as_the_source_had_it(tmp_path, ocr):
    """Q5: an OCR-only redaction writes DERIVED and 113062, never
    `(0028,0301) NO`. Kills `_apply_redaction_flags` called unchanged."""
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(_cohort(
            tmp_path, (ONE, {"ink": [(0, *INK_BOX)], "burned_in": "YES"}),
            (TWO, {"ink": [(0, *INK_BOX)], "series_uid": SERIES + ".9"})))
        session.load_config(_config(tmp_path, "redact"))
        assert session.redact(show_progress=False) == 2
        by_series = {se.series_instance_uid: se.instances[0]
                     for se in session.store.patients[0].studies[0].series}
        said_yes = by_series[SERIES]
        said_nothing = by_series[SERIES + ".9"]
        assert said_yes.attributes["0028,0301"] == "YES"
        assert "0028,0301" not in said_nothing.attributes
        for inst in (said_yes, said_nothing):
            assert inst.attributes["0008,0008"][0] == "DERIVED"
            [item] = inst.sequences["0008,9215"].items
            assert item.attributes["0008,0100"] == "113062"
        out = tmp_path / "out"
        session.export(str(out), use_compression=False, show_progress=False)
    values = sorted(str(pydicom.dcmread(p).get("BurnedInAnnotation"))
                    for p in exported_files(out))
    assert values == ["None", "YES"]


@pytest.mark.parametrize("conf, blanked", [(79, False), (80, True)])
def test_the_confidence_threshold_is_inclusive_at_the_default(tmp_path, ocr, conf, blanked):
    ocr.conf = conf
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)]})))
        session.load_config(_config(tmp_path, "redact"))
        assert session.redact(show_progress=False) == int(blanked)
        assert _the_box(_only_instance(session).get_pixel_data()[None]).any() != blanked


def test_two_letters_are_text(tmp_path, ocr):
    """Technologist initials (`JT`) were hits in the owner's measurement;
    the scan's `<= 2` characters noise filter must not apply here."""
    ocr.text = "JT"
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)]})))
        session.load_config(_config(tmp_path, "redact"))
        assert session.redact(show_progress=False) == 1


def test_a_box_blanks_only_its_own_frame(tmp_path, ocr):
    """Per-frame boxes: `apply_redaction_to_array` zeroes a zone on every
    frame, so a box applied through it would blank across the cine."""
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"frames": 3, "ink": [(1, *INK_BOX)]})))
        session.load_config(_config(tmp_path, "redact"))
        assert session.redact(show_progress=False) == 1
        live = _only_instance(session).get_pixel_data()
        assert (live[0] == BACKGROUND).all() and (live[2] == BACKGROUND).all()
        assert not _the_box(live, frame=1).any()
        assert _record(_only_instance(session))["boxes"] == [[1, *PADDED]]


def test_a_zone_is_applied_before_the_read(tmp_path, ocr):
    """Text a zone covers is not found again, so `report` and `withhold`
    do not speak for what the export will blank anyway."""
    zone = (f"machines:\n  - serial_number: SN-1\n    redaction_zones:\n"
            f"      - [{PADDED[0]}, {PADDED[1]}, {PADDED[2]}, {PADDED[3]}]\n")
    db = tmp_path / "s.db"
    with Session(str(db)) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)], "serial": "SN-1"})))
        session.load_config(_config(tmp_path, "withhold", zone))
        session.redact(show_progress=False)
        assert _record(_only_instance(session))["boxes"] == []
        out = tmp_path / "out"
        session.export(str(out), use_compression=False, show_progress=False)
    assert len(exported_files(out)) == 1


def test_a_second_pass_reads_nothing_and_moves_no_uid(tmp_path, ocr):
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)]})))
        session.load_config(_config(tmp_path, "redact"))
        session.redact(show_progress=False)
        uid = _only_instance(session).sop_instance_uid
        ocr.calls = 0
        assert session.redact(show_progress=False) == 0
        assert ocr.calls == 0
        assert _only_instance(session).sop_instance_uid == uid


def test_force_reads_again_and_the_uid_follows_the_union(tmp_path, ocr):
    """`force=True` re-reads the frame the instance holds (already blanked)
    and keeps every box it recorded, so the hash names every zero region."""
    second = (40, 50, 10, 20)
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)]})))
        session.load_config(_config(tmp_path, "redact"))
        session.redact(show_progress=False)
        inst = _only_instance(session)
        first_uid = inst.sop_instance_uid
        arr = inst.get_pixel_data().copy()
        arr[second[0]:second[1], second[2]:second[3]] = INK
        inst.set_pixel_data(arr)
        ocr.calls = 0
        assert session.redact(show_progress=False, force=True) == 1
        assert ocr.calls > 0
        boxes = _record(inst)["boxes"]
        assert [0, *PADDED] in boxes and [0, 38, 52, 8, 22] in boxes
        assert inst.sop_instance_uid != first_uid


def test_a_box_still_found_after_redaction_writes_a_residual_row(tmp_path, ocr):
    ocr.sticky = True
    db = tmp_path / "s.db"
    with Session(str(db)) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)]})))
        session.load_config(_config(tmp_path, "redact"))
        session.redact(show_progress=False)
        uid = _only_instance(session).sop_instance_uid
    residual = [r for r in audit_rows(db)
                if r[0] == "WARNING" and "still found after redaction" in r[2]]
    assert residual == [(
        "WARNING", uid,
        f"burned_in_text: 1 text region(s) still found after redaction on "
        f"frame(s) 0 of instance {uid}.")]


def test_one_redaction_row_per_pass_naming_counts_and_tesseract(tmp_path, ocr):
    db = tmp_path / "s.db"
    with Session(str(db)) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)]}),
                               (TWO, {})))
        session.load_config(_config(tmp_path, "redact"))
        session.redact(show_progress=False)
    rows = [r for r in audit_rows(db) if r[0] == "REDACTION"]
    assert rows == [(
        "REDACTION", "burned_in_text",
        "burned_in_text (action redact, min_confidence 80): read 2 of 2 "
        "instance(s) holding pixels, 0 could not be read, 0 already checked "
        "under this policy; text found on 1 (1 region(s)); 1 redacted. OCR by "
        "tesseract 5.5.3.")]


# --- report and withhold ----------------------------------------------------

def test_report_blanks_nothing_and_writes_a_row_per_instance_with_text(tmp_path, ocr):
    db = tmp_path / "s.db"
    with Session(str(db)) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)]}),
                               (TWO, {})))
        session.load_config(_config(tmp_path, "report"))
        session.audit()
        session.anonymize()
        assert session.redact(show_progress=False) == 0
        found = next(i for se in session.store.patients[0].studies[0].series
                     for i in se.instances if _record(i)["boxes"])
        uid = found.sop_instance_uid
        out = tmp_path / "out"
        session.export(str(out), use_compression=False, show_progress=False)
        session.generate_report(str(tmp_path / "r.md"))
    assert len(exported_files(out)) == 2
    assert any((pixels_of(p) == INK).any() for p in exported_files(out))
    rows = [r for r in audit_rows(db) if r[0] == "WARNING"]
    # One from the pass that found it, one from the export that wrote it.
    assert rows == [
        ("WARNING", uid,
         f"burned_in_text: 1 text region(s) found on frame(s) 0 of instance "
         f"{uid}; not redacted (action: report)."),
        ("WARNING", uid,
         f"DICOM export to {out} did not withhold instance {uid}: burned_in_text found 1 "
         f"text region(s) on frame(s) 0, not redacted (action: report).")]
    assert REVIEW_LINE in (tmp_path / "r.md").read_text()


def test_boxes_read_under_one_action_serve_another(tmp_path, ocr):
    """The record is current under the threshold and the zones, not the
    action: a store read under `report` is not read again under `redact`.
    Until `redact()` blanks what the record holds, the export withholds it.
    Kills the gate's check that a redaction carrying the boxes landed."""
    db = tmp_path / "s.db"
    with Session(str(db)) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)]})))
        session.load_config(_config(tmp_path, "report"))
        session.redact(show_progress=False)
        session.load_config(_config(tmp_path, "redact", name="r.yaml"))
        out = tmp_path / "out"
        first = session.export(str(out), use_compression=False, show_progress=False)
        ocr.calls = 0
        assert session.redact(show_progress=False) == 1
        assert ocr.calls == 1          # the residual read, and only it
        second = session.export(str(tmp_path / "out2"), use_compression=False,
                                show_progress=False)
    assert first.written == 0 and second.written == 1
    assert ("WARNING", ONE,
            f"DICOM export to {out} withheld instance {ONE}: burned_in_text "
            f"found 1 text region(s) on frame(s) 0 that no redaction has "
            f"blanked; run redact().") in audit_rows(db)


def test_withhold_keeps_the_instance_out_of_the_export_and_says_so(tmp_path, ocr):
    db = tmp_path / "s.db"
    with Session(str(db)) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)]}),
                               (TWO, {})))
        session.load_config(_config(tmp_path, "withhold"))
        assert session.redact(show_progress=False) == 0
        out = tmp_path / "out"
        summary = session.export(str(out), use_compression=False, show_progress=False)
    assert summary.written == 1
    assert [pydicom.dcmread(p).SOPInstanceUID for p in exported_files(out)] != []
    assert ONE not in summary.written_uids
    rows = audit_rows(db)
    assert ("WARNING", ONE,
            f"DICOM export to {out} withheld instance {ONE}: burned_in_text "
            f"found 1 text region(s) on frame(s) 0 (action: withhold).") in rows
    [export_row] = [r for r in rows if r[0] == "EXPORT"]
    assert export_row[2] == (
        f"DICOM export to {out}: wrote 1 of 1 planned instances from 1 "
        f"patients; 1 more withheld by the burned_in_text policy.")


@pytest.mark.parametrize("action, written", [("redact", 0), ("withhold", 0), ("report", 1)])
def test_an_instance_never_checked_under_the_policy_is_gated(tmp_path, ocr, action, written):
    """`redact()` never called after `load_config()`: fail closed for the
    two actions that promise something about pixels."""
    db = tmp_path / "s.db"
    with Session(str(db)) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)]})))
        session.load_config(_config(tmp_path, action))
        out = tmp_path / "out"
        summary = session.export(str(out), use_compression=False, show_progress=False)
    assert summary.written == written
    verb = "did not withhold" if written else "withheld"
    assert ("WARNING", ONE,
            f"DICOM export to {out} {verb} instance {ONE}: its pixels were not "
            f"checked for burned-in text under this configuration's "
            f"burned_in_text policy; run redact().") in audit_rows(db)


def test_the_record_survives_a_reopen(tmp_path, ocr):
    """The record rides `attributes_json`: a reopened store is not read
    again, and its export is gated by what the earlier session found."""
    db = tmp_path / "s.db"
    cfg = _config(tmp_path, "redact")
    with Session(str(db)) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)]}),
                               (TWO, {"series_uid": SERIES + ".9"})))
        session.load_config(cfg)
        session.redact(show_progress=False)
        session.save(sync=True)
    ocr.calls = 0
    with Session(str(db)) as session:
        session.load_config(cfg)
        assert session.redact(show_progress=False) == 0
        records = [_record(i) for se in session.store.patients[0].studies[0].series
                   for i in se.instances]
        summary = session.export(str(tmp_path / "out"), use_compression=False,
                                 show_progress=False)
    assert ocr.calls == 0
    assert sorted(len(r["boxes"]) for r in records) == [0, 1]
    assert summary.written == 2


def test_a_record_under_another_policy_is_not_a_check(tmp_path, ocr):
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(_cohort(tmp_path, (TWO, {})))
        session.load_config(_config(tmp_path, "withhold"))
        session.redact(show_progress=False)
        session.load_config(_config(tmp_path, "withhold", "  min_confidence: 90\n", "d.yaml"))
        summary = session.export(str(tmp_path / "out"), use_compression=False,
                                 show_progress=False)
    assert summary.written == 0


def test_an_instance_with_no_pixels_is_never_gated_or_counted(tmp_path, ocr):
    src = tmp_path / "in"
    src.mkdir()
    write_sr(src / "sr.dcm", sop_uid=TWO)
    db = tmp_path / "s.db"
    with Session(str(db)) as session:
        session.ingest(str(src))
        session.load_config(_config(tmp_path, "withhold"))
        assert ocr.calls == 0
        summary = session.export(str(tmp_path / "out"), use_compression=False,
                                 show_progress=False)
        session.redact(show_progress=False)
        summary_after = session.export(str(tmp_path / "out2"), use_compression=False,
                                       show_progress=False)
    assert summary.written == 1 and summary_after.written == 1
    assert ocr.calls == 0
    assert not [r for r in audit_rows(db) if r[0] == "WARNING"]


def test_a_clean_redact_pass_grades_pass_and_section_5_names_it(tmp_path, ocr):
    """Q4: blanking text does not cost the PASS by itself. Kills the B row
    missing (condition 6 would hold) and section 5 still saying no OCR ran."""
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)]})))
        session.load_config(_config(tmp_path, "redact"))
        session.audit()
        session.anonymize()
        session.redact(show_progress=False)
        session.export(str(tmp_path / "out"), use_compression=False, show_progress=False)
        session.generate_report(str(tmp_path / "r.md"))
    report = (tmp_path / "r.md").read_text()
    assert PASS_LINE in report, report
    [line] = [l for l in report.splitlines() if "Burned-in Text" in l]
    assert "redact()" in line and "read 1 of 1" in line and "1 redacted" in line
    assert "does not say burned-in text was checked by OCR" not in report


def test_a_report_pass_that_finds_nothing_still_writes_its_row(tmp_path, ocr):
    db = tmp_path / "s.db"
    with Session(str(db)) as session:
        session.ingest(_cohort(tmp_path, (TWO, {})))
        session.load_config(_config(tmp_path, "report"))
        session.audit()
        session.anonymize()
        session.redact(show_progress=False)
        session.generate_report(str(tmp_path / "r.md"))
    assert [r[1] for r in audit_rows(db) if r[0] == "REDACTION"] == ["burned_in_text"]
    assert PASS_LINE in (tmp_path / "r.md").read_text()


def test_the_phi_status_is_carried_across_the_record(tmp_path, ocr):
    """The record is the pass's own bookkeeping: an instance it checked
    and found clean keeps the status `audit()`/`anonymize()` gave it."""
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(_cohort(tmp_path, (TWO, {}), (ONE, {"ink": [(0, *INK_BOX)]})))
        session.load_config(_config(tmp_path, "redact"))
        session.audit()
        session.anonymize()
        before = {id(i): i.phi_status for se in session.store.patients[0].studies[0].series
                  for i in se.instances}
        session.redact(show_progress=False)
        after = {id(i): i.phi_status for se in session.store.patients[0].studies[0].series
                 for i in se.instances}
    assert after == before


def test_an_unreadable_instance_is_a_warning_row_and_is_gated(tmp_path, ocr):
    ocr.fail = True
    db = tmp_path / "s.db"
    with Session(str(db)) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)]})))
        session.load_config(_config(tmp_path, "redact"))
        assert session.redact(show_progress=False) == 0
        summary = session.export(str(tmp_path / "out"), use_compression=False,
                                 show_progress=False)
    assert summary.written == 0
    unread = [r for r in audit_rows(db) if r[0] == "WARNING" and "could not read" in r[2]]
    assert len(unread) == 1 and unread[0][1] == ONE
    assert "redact()" in unread[0][2]


# --- doors that must not run it, and the refusal ---------------------------

def test_redact_with_zones_and_no_policy_calls_no_ocr(tmp_path, ocr):
    zone = "machines:\n  - serial_number: SN-1\n    redaction_zones: [[0, 4, 0, 4]]\n"
    cfg = tmp_path / "z.yaml"
    cfg.write_text(zone, encoding="utf-8")
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)], "serial": "SN-1"})))
        session.load_config(str(cfg))
        assert session.redact(show_progress=False) == 1
        assert services.BURNED_IN_TEXT_ATTR not in _only_instance(session).attributes
    assert ocr.calls == 0


def test_redact_by_machine_does_not_run_the_pass(tmp_path, ocr):
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)], "serial": "SN-1"})))
        session.load_config(_config(tmp_path, "redact"))
        session.redact_by_machine("SN-1", [0, 4, 0, 4])
        assert session.configuration.burned_in_text == {"action": "redact",
                                                        "min_confidence": 80}
        assert services.BURNED_IN_TEXT_ATTR not in _only_instance(session).attributes
    assert ocr.calls == 0


def test_redact_refuses_before_anything_when_ocr_cannot_run(tmp_path, monkeypatch):
    """With a zone configured too: one pass, all or nothing."""
    monkeypatch.setattr(pixel_analysis, "HAS_OCR", False)
    monkeypatch.setattr(pixel_analysis, "pytesseract", None)
    zone = "machines:\n  - serial_number: SN-1\n    redaction_zones: [[0, 4, 0, 4]]\n"
    db = tmp_path / "s.db"
    with Session(str(db)) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)], "serial": "SN-1"})))
        session.load_config(_config(tmp_path, "redact", zone))
        with pytest.raises(pixel_analysis.OcrUnavailableError, match="redact()"):
            session.redact(show_progress=False)
        assert _only_instance(session).sop_instance_uid == ONE
        assert "_ISOCENTER_REDACTION_HASH" not in _only_instance(session).attributes
    assert not [r for r in audit_rows(db) if r[0] == "REDACTION"]


def test_a_policy_assigned_in_code_is_judged_before_the_pass(tmp_path, ocr):
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)]})))
        session.configuration.burned_in_text = {"action": "blank"}
        with pytest.raises(ValueError, match="session.configuration"):
            session.redact(show_progress=False)
    assert ocr.calls == 0


# --- no text anywhere (#1108 security rule 1) --------------------------------

def test_the_text_ocr_read_reaches_no_row_log_report_store_or_outcome(tmp_path, ocr, caplog):
    caplog.set_level(logging.DEBUG)
    db = tmp_path / "s.db"
    with Session(str(db)) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)]}),
                               (TWO, {"ink": [(0, 2, 6, 2, 6)],
                                      "series_uid": SERIES + ".9"})))
        session.load_config(_config(tmp_path, "report"))
        session.redact(show_progress=False)
        session.load_config(_config(tmp_path, "redact", name="r.yaml"))
        ocr.sticky = True
        session.redact(show_progress=False)
        outcome = session_module._burned_in_text_worker(
            (_only_instance(session), (), 80, None, None))
        session.export(str(tmp_path / "out"), use_compression=False, show_progress=False)
        session.generate_report(str(tmp_path / "r.md"))
        session.save(sync=True)
    assert SENTINEL not in repr(outcome)
    assert "text" not in {f for f in getattr(type(outcome), "_fields", ())}
    assert not any(SENTINEL in str(cell) for row in audit_rows(db) for cell in row)
    assert SENTINEL not in (tmp_path / "r.md").read_text()
    assert SENTINEL not in caplog.text
    with sqlite3.connect(str(db)) as conn:
        dump = "\n".join(conn.iterdump())
    assert SENTINEL not in dump
    assert ocr.calls > 0


def test_a_failing_tesseract_quotes_no_text_either(tmp_path, ocr, caplog):
    """A tesseract whose failure message holds text: the pass names the
    frame and the exception's class, never its message (review O7: with
    a message that could not hold the sentinel, this could not fail)."""
    caplog.set_level(logging.DEBUG)
    ocr.fail = True
    ocr.fail_message = f"tesseract exited 1: read {SENTINEL} then failed"
    db = tmp_path / "s.db"
    with Session(str(db)) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)]})))
        session.load_config(_config(tmp_path, "redact"))
        session.redact(show_progress=False)
    rows = audit_rows(db)
    assert [r for r in rows if "OCR failed on frame 0: RuntimeError" in r[2]]
    assert not any(SENTINEL in str(cell) for row in rows for cell in row)
    assert SENTINEL not in caplog.text


# --- review of #1109: the lines nothing pinned (B1-B3, O1, O4, O5) ----------

def _never_checked(out, uid, written):
    verb = "did not withhold" if written else "withheld"
    return ("WARNING", uid,
            f"DICOM export to {out} {verb} instance {uid}: its pixels were not "
            f"checked for burned-in text under this configuration's "
            f"burned_in_text policy; run redact().")


@pytest.mark.parametrize("action", ["redact", "withhold"])
def test_one_frame_that_cannot_be_read_leaves_no_record(tmp_path, ocr, action):
    """B1: three frames, OCR fails on the second only, so the instance
    *was* read. A frame not read is a frame not checked: no record, one
    unread row, withheld at export. Kills the `continue` after the
    failure is noted, which `fail` on every frame hid behind `read`."""
    ocr.fail_calls = {2}
    db = tmp_path / "s.db"
    out = tmp_path / "out"
    with Session(str(db)) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"frames": 3, "ink": [(0, *INK_BOX)]})))
        session.load_config(_config(tmp_path, action))
        assert session.redact(show_progress=False) == 0
        inst = _only_instance(session)
        assert services.BURNED_IN_TEXT_ATTR not in inst.attributes
        assert "_ISOCENTER_REDACTION_HASH" not in inst.attributes
        summary = session.export(str(out), use_compression=False, show_progress=False)
    assert summary.written == 0
    rows = audit_rows(db)
    unread = [r for r in rows if r[0] == "WARNING" and "could not read" in r[2]]
    assert [r[1] for r in unread] == [ONE]
    assert _never_checked(out, ONE, False) in rows


@pytest.mark.parametrize("zone_first", [True, False])
def test_a_zone_removed_or_added_after_the_read_stales_the_record(tmp_path, ocr, zone_first):
    """B2: the record speaks for the zones it was read under. A zone over
    the text, removed after a `withhold` read, would otherwise leave a
    current record with no boxes and the text exported unread; a zone
    added makes the record stale the same way."""
    zone = (f"machines:\n  - serial_number: SN-1\n    redaction_zones:\n"
            f"      - [{PADDED[0]}, {PADDED[1]}, {PADDED[2]}, {PADDED[3]}]\n")
    first, second = (zone, "") if zone_first else ("", zone)
    out = tmp_path / "out"
    db = tmp_path / "s.db"
    with Session(str(db)) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)], "serial": "SN-1"})))
        session.load_config(_config(tmp_path, "withhold", first))
        session.redact(show_progress=False)
        # The zone itself redacts the stored pixels when it comes first,
        # and the instance takes a new UID.
        uid = _only_instance(session).sop_instance_uid
        session.load_config(_config(tmp_path, "withhold", second, "d.yaml"))
        summary = session.export(str(out), use_compression=False, show_progress=False)
    assert summary.written == 0
    assert _never_checked(out, uid, False) in audit_rows(db)


def test_a_zone_added_over_zeros_still_reads_again(tmp_path, ocr):
    """B2, the hash's own term: a zone added exactly over a box `redact()`
    already blanked moves no pixel the read sees, so only the zones in
    the policy hash say the record was read under other zones. Kills
    `"zones": []` in `_burned_in_text_policy_hash`, which the pixel digest
    alone would not."""
    zone = (f"machines:\n  - serial_number: SN-1\n    redaction_zones:\n"
            f"      - [{PADDED[0]}, {PADDED[1]}, {PADDED[2]}, {PADDED[3]}]\n")
    db = tmp_path / "s.db"
    with Session(str(db)) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)], "serial": "SN-1"})))
        session.load_config(_config(tmp_path, "redact"))
        session.redact(show_progress=False)
        session.load_config(_config(tmp_path, "redact", zone, "d.yaml"))
        session.redact(show_progress=False)
    passes = [r[2] for r in audit_rows(db) if r[0] == "REDACTION"
              and r[1] == "burned_in_text"]
    assert "read 1 of 1 instance(s) holding pixels, 0 could not be read, 0 already" in passes[-1]


def test_the_padding_is_fifteen_percent_of_the_box_height(tmp_path, ocr):
    """B3: a 20-pixel box pads by round(3.0) = 3, past the 2-pixel floor,
    so the factor is told apart from the floor that a 10-pixel box hits."""
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, 10, 30, 30, 60)]})))
        session.load_config(_config(tmp_path, "redact"))
        session.redact(show_progress=False)
        assert _record(_only_instance(session))["boxes"] == [[0, 7, 33, 27, 63]]


# --- the record is tied to the pixels it read (owner ruling, review of #1109) ---

def _replace_pixels(inst, box=(40, 50, 10, 20)):
    arr = inst.get_pixel_data().copy()
    arr[box[0]:box[1], box[2]:box[3]] = INK
    inst.set_pixel_data(arr)


@pytest.mark.parametrize("action, written", [("redact", 0), ("withhold", 0), ("report", 1)])
def test_pixels_replaced_after_redact_are_never_checked(tmp_path, ocr, action, written):
    """The record names the pixels the read saw, by digest. Pixels set
    after `redact()` are pixels no read saw: the export says so, as for
    an instance never checked, whatever the record's boxes say."""
    db = tmp_path / "s.db"
    out = tmp_path / "out"
    with Session(str(db)) as session:
        session.ingest(_cohort(tmp_path, (ONE, {})))
        session.load_config(_config(tmp_path, action))
        session.redact(show_progress=False)
        inst = _only_instance(session)
        assert _record(inst)["boxes"] == []
        _replace_pixels(inst)
        summary = session.export(str(out), use_compression=False, show_progress=False)
    assert summary.written == written
    assert _never_checked(out, ONE, written) in audit_rows(db)


@pytest.mark.parametrize("action", ["redact", "withhold", "report"])
def test_pixels_left_as_read_pass_the_gate(tmp_path, ocr, action):
    """The control: the same image with its pixels as read is written,
    with no row, under every action; under `redact` after its own
    redaction too, whose read afterwards is what the digest names."""
    ink = [(0, *INK_BOX)] if action == "redact" else []
    db = tmp_path / "s.db"
    with Session(str(db)) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": ink})))
        session.load_config(_config(tmp_path, action))
        session.redact(show_progress=False)
        summary = session.export(str(tmp_path / "out"), use_compression=False,
                                 show_progress=False)
    assert summary.written == 1
    assert not [r for r in audit_rows(db) if r[0] == "WARNING"]


def test_pixels_replaced_after_a_reopen_are_never_checked(tmp_path, ocr):
    db = tmp_path / "s.db"
    out = tmp_path / "out"
    cfg = _config(tmp_path, "redact")
    with Session(str(db)) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)]})))
        session.load_config(cfg)
        session.redact(show_progress=False)
        session.save(sync=True)
    with Session(str(db)) as session:
        session.load_config(cfg)
        inst = _only_instance(session)
        uid = inst.sop_instance_uid
        _replace_pixels(inst)
        summary = session.export(str(out), use_compression=False, show_progress=False)
    assert summary.written == 0
    assert _never_checked(out, uid, False) in audit_rows(db)


def test_redact_reads_pixels_replaced_since_its_last_read(tmp_path, ocr):
    """Not only the export: the next `redact()` reads the new pixels
    without `force`, finds the new text and blanks it."""
    box = (40, 50, 10, 20)
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(_cohort(tmp_path, (ONE, {})))
        session.load_config(_config(tmp_path, "redact"))
        session.redact(show_progress=False)
        inst = _only_instance(session)
        _replace_pixels(inst, box)
        ocr.calls = 0
        assert session.redact(show_progress=False) == 1
        assert ocr.calls > 0
        assert _record(inst)["boxes"] == [[0, 38, 52, 8, 22]]
        summary = session.export(str(tmp_path / "out"), use_compression=False,
                                 show_progress=False)
    assert summary.written == 1


def test_the_digest_is_over_the_pixels_after_the_zones(tmp_path, ocr):
    """What the read saw: the zones zeroed on a copy. So a zone's own
    redaction, which zeroes exactly what the read already saw as zero,
    leaves the record current."""
    zone = "machines:\n  - serial_number: SN-1\n    redaction_zones: [[0, 4, 0, 4]]\n"
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"serial": "SN-1"})))
        session.load_config(_config(tmp_path, "withhold", zone))
        assert session.redact(show_progress=False) == 1
        inst = _only_instance(session)
        zoned = inst.get_pixel_data().copy()
        assert pixel_analysis._read_digest(zoned) == _record(inst)["pixels"]
        summary = session.export(str(tmp_path / "out"), use_compression=False,
                                 show_progress=False)
    assert summary.written == 1


def test_a_read_after_redaction_that_fails_writes_a_row_and_stales_the_record(tmp_path, ocr):
    """O5: the read after redaction is what names the redacted pixels. When
    it fails, a row says so and the export treats the instance as never
    checked, rather than as checked by a read of the pixels it replaced."""
    ocr.fail_calls = {2}
    db = tmp_path / "s.db"
    out = tmp_path / "out"
    with Session(str(db)) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)]})))
        session.load_config(_config(tmp_path, "redact"))
        assert session.redact(show_progress=False) == 1
        uid = _only_instance(session).sop_instance_uid
        summary = session.export(str(out), use_compression=False, show_progress=False)
    rows = audit_rows(db)
    assert ("WARNING", uid,
            f"burned_in_text: the read after redaction could not read instance "
            f"{uid} in full, so its redacted pixels are not checked under this "
            f"policy.") in rows
    assert summary.written == 0
    assert _never_checked(out, uid, False) in rows


def test_a_second_report_pass_writes_no_second_row(tmp_path, ocr):
    """O1: the report row belongs to the read that found the text."""
    db = tmp_path / "s.db"
    with Session(str(db)) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)]})))
        session.load_config(_config(tmp_path, "report"))
        session.redact(show_progress=False)
        session.redact(show_progress=False)
    rows = [r for r in audit_rows(db) if "(action: report)" in r[2]]
    assert len(rows) == 1


def test_tesseract_runs_with_one_thread_and_the_variable_is_put_back(tmp_path, ocr, monkeypatch):
    """O1: `OMP_THREAD_LIMIT=1` while the pass reads, as found afterwards."""
    monkeypatch.delenv("OMP_THREAD_LIMIT", raising=False)
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)]})))
        session.load_config(_config(tmp_path, "redact"))
        session.redact(show_progress=False)
    assert ocr.omp_seen and set(ocr.omp_seen) == {"1"}
    assert "OMP_THREAD_LIMIT" not in os.environ


def test_the_digest_names_the_dtype_and_the_shape_not_only_the_bytes():
    """The same bytes handed back as another dtype (an int8 view of a
    uint8 frame) or another shape are other pixels to OCR, and the
    sidecar's dedup keeps them as the same frame: the digest's prefix is
    what tells them apart (review of #1109, optional item)."""
    arr = np.arange(64 * 96, dtype=np.uint8).reshape(64, 96)
    signed = arr.view(np.int8)
    turned = arr.reshape(96, 64)
    assert arr.tobytes() == signed.tobytes() == turned.tobytes()
    digests = {pixel_analysis._read_digest(a) for a in (arr, signed, turned)}
    assert len(digests) == 3


def test_eighty_and_eighty_point_zero_are_one_policy():
    """O4: one threshold, one hash, however YAML spelled it."""
    assert (services._burned_in_text_policy_hash(
                {"action": "redact", "min_confidence": 80}, [])
            == services._burned_in_text_policy_hash(
                {"action": "redact", "min_confidence": 80.0}, []))


# --- A zone task under a `""` or `match:` rule (#1108 parts A and C) -------

@pytest.mark.parametrize("rule, selector", [
    ('  - serial_number: ""\n', ""),
    ("  - match: {manufacturer: ACME}\n", {"match": {"manufacturer": "ACME"}}),
], ids=["empty", "match"])
def test_boxes_join_a_serial_less_zone_task_under_its_selector(tmp_path, ocr, rule, selector):
    """The reconciliation of #1109 and #1111: a serial-less image a `""` or
    `match:` rule zones, with text the zone misses. The boxes join that
    rule's zone task, and the hash is over the rule's selector, the zone
    and the boxes. Kills a `match` selector refused by the hash, and a
    zone task under these selectors not found by the fold."""
    zone = [0, 4, 0, 4]
    extra = f"machines:\n{rule}    redaction_zones:\n      - {zone}\n"
    with Session(str(tmp_path / "s.db")) as session:
        load_fixed_secret(session)
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)]})))
        session.load_config(_config(tmp_path, "redact", extra))
        assert session.redact(show_progress=False) == 1
        inst = _only_instance(session)
        record = _record(inst)
        assert record["boxes"]
        expected_hash = services._redaction_config_hash(
            [selector], [zone], ocr=(record["policy"], record["boxes"]))
        assert inst.attributes["_ISOCENTER_REDACTION_HASH"] == expected_hash
        assert inst.sop_instance_uid == privacy._redaction_uid_for(
            ONE, expected_hash, FIXED_A)
        live = inst.get_pixel_data()[None]
        assert not _the_box(live).any()
        assert not live[0, 0:4, 0:4].any()

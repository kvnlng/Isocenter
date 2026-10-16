"""`detector: db` beside a narrowed `target`: each region the detector
finds is cut out, levelled, enlarged and read by tesseract as one line,
and the reading is classified by PR B's kinds (#1121, owner ruling (b) of
2026-10-15).

The rulings these tests hold:

- the crop is the region's rotated rectangle plus 0.30 of its height,
  levelled by its angle, enlarged 2x, bordered by 12 replicated pixels and
  read with `--psm 7` (the spike's constants: a reasonable pick among
  variants that differed by noise);
- a region whose reading holds a named kind is redacted; one whose
  reading holds fewer than two letters or digits cannot be classified and
  is redacted too, counted as unread (Q-B10 (a)); any other is left;
- so a narrowed target never redacts a box `any` would not;
- tesseract is required (and the names model under `names`); under
  `target: any` it is not;
- nothing read leaves the worker.

**How the readers are faked.** `RegionDetector` stands in for the
detector's seam and returns a straight quad for each region of `REGIONS`
whose ink is still on the frame. `CropTesseract` stands in for pytesseract
and tells the regions apart by the width of the ink in the crop it is
handed (the regions' widths differ by 10 px), so a crop of the wrong place
reads as nothing it knows. Threads only.
"""
import itertools
import logging
from types import SimpleNamespace

import numpy as np
import pytest

from isocenter import pixel_analysis, services, text_detection, text_kinds
from isocenter.pixel_analysis import OcrUnavailableError
from isocenter.session import DicomSession as Session

from test_redact_blanks_burned_in_text import (
    INK, audit_rows, exported_files, pixels_of, write_image)
from test_text_kinds import FakeNlp

STUB_OPENCV = "5.0.0-stub"
SENTINEL = "QZXJ7CROPSENTINEL"
UID = "1.2.826.0.1.3680043.8.498.1121.30"
OTHER = "1.2.826.0.1.3680043.8.498.1121.31"

#: `(x1, y1, x2, y2)` on the 64 x 96 test frame, each 8 px tall, each a
#: distinct width, with what tesseract reads in it.
ID_BOX = (4, 4, 24, 12)          # 20 wide
DATE_BOX = (30, 4, 60, 12)       # 30 wide
LABEL_BOX = (4, 30, 14, 38)      # 10 wide
BLANK_BOX = (30, 30, 70, 38)     # 40 wide: reads as nothing
LETTER_BOX = (20, 48, 70, 56)    # 50 wide: one letter
READINGS = {20: "MRN0012345", 30: "01/01/1970", 10: SENTINEL, 40: "",
            50: "L"}
REGIONS = [ID_BOX, DATE_BOX, LABEL_BOX, BLANK_BOX, LETTER_BOX]


def _ink(*boxes):
    return [(0, y1, y2, x1, x2) for x1, y1, x2, y2 in boxes]


class RegionDetector:
    """`text_detection._detect_boxes` returning a straight quad for each
    region of `REGIONS` whose ink is still there, in that order."""

    def __init__(self):
        self.calls = 0
        self.regions = REGIONS

    def __call__(self, image, input_size, min_score):
        self.calls += 1
        # The seam is handed the frame inside its border, and answers in
        # that image's coordinates.
        b = text_detection.DETECTION_BORDER
        grey = image[b:-b, b:-b, 0]
        found = []
        for x1, y1, x2, y2 in self.regions:
            if (grey[y1:y2, x1:x2] == INK).any():
                found.append((np.array([[x1, y1], [x2, y1], [x2, y2], [x1, y2]],
                                       dtype=float) + b, 0.97))
        return found


class CropTesseract:
    """A pytesseract stand-in for the crop reads: the reading of the region
    whose ink width the crop holds, after the 2x enlargement."""

    def __init__(self):
        self.calls = []
        self.fail = None
        self.readings = READINGS
        self.Output = SimpleNamespace(DICT="dict")
        self.pytesseract = SimpleNamespace(tesseract_cmd="tesseract")

    @staticmethod
    def get_tesseract_version():
        return "5.5.3"

    def image_to_data(self, img, config=None, output_type=None, **kwargs):
        arr = np.asarray(img)
        self.calls.append((img.size, config))
        if self.fail is not None:
            raise self.fail
        width = int((arr > 200).any(axis=0).sum()) / 2
        reading = self.readings.get(int(round(width / 10.0)) * 10, "?")
        words = reading.split() or [""]
        return {"text": words, "conf": [95] * len(words)}


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


@pytest.fixture
def detector(monkeypatch):
    fake = RegionDetector()
    monkeypatch.setattr(text_detection, "HAS_DETECTOR", True)
    monkeypatch.setattr(text_detection, "cv2",
                        SimpleNamespace(__version__=STUB_OPENCV))
    monkeypatch.setattr(text_detection, "_detector_unavailable_reason",
                        lambda: None)
    monkeypatch.setattr(text_detection, "_detect_boxes", fake)
    return fake


@pytest.fixture
def reader(monkeypatch):
    fake = CropTesseract()
    monkeypatch.setattr(pixel_analysis, "HAS_OCR", True)
    monkeypatch.setattr(pixel_analysis, "pytesseract", fake)
    monkeypatch.setattr(pixel_analysis, "_ocr_unavailable_reason", lambda: None)
    return fake


@pytest.fixture
def no_reader(monkeypatch):
    monkeypatch.setattr(pixel_analysis, "HAS_OCR", False)
    monkeypatch.setattr(pixel_analysis, "pytesseract", None)


def _config(tmp_path, action="redact", target="[dates, ids]"):
    path = tmp_path / "c.yaml"
    path.write_text(f"privacy_profile: basic\nburned_in_text:\n  action: {action}\n"
                    f"  scope: all\n  detector: db\n  target: {target}\n",
                    encoding="utf-8")
    return str(path)


def _cohort(tmp_path, *images):
    src = tmp_path / "in"
    src.mkdir(exist_ok=True)
    for i, (uid, boxes) in enumerate(images):
        write_image(src / f"i{i}.dcm", sop_uid=uid, ink=_ink(*boxes))
    return str(src)


def _only_instance(session):
    return session.store.patients[0].studies[0].series[0].instances[0]


def _holds_ink(arr, box):
    x1, y1, x2, y2 = box
    return bool((arr[y1:y2, x1:x2] == INK).any())


# --- the pass ----------------------------------------------------------------

def test_a_region_is_redacted_for_its_kind_or_because_it_cannot_be_read(
        tmp_path, detector, reader):
    """Kills: no crop read (every region left, or every region redacted),
    the unreadable arm dropped (Q-B10) or flipped, and a reading that
    reaches no classifier."""
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(_cohort(tmp_path, (UID, REGIONS)))
        session.load_config(_config(tmp_path))
        session.redact(show_progress=False)
        session.export(str(tmp_path / "out"), use_compression=False,
                       show_progress=False)
    [path] = exported_files(tmp_path / "out")
    arr = pixels_of(path)[0]
    assert [_holds_ink(arr, box) for box in REGIONS] == [
        False, False, True, False, False]
    # One read per region, then the read after redaction, which finds the
    # label left in place and reads it again.
    assert len(reader.calls) == 6


def test_the_crop_is_read_as_one_line_enlarged_and_bordered(tmp_path, detector, reader):
    """The image tesseract is handed for the 10 x 8 label: (8 + 2 x 2 pad)
    x (10 + 2 x 2 pad), twice that, plus 12 px each side. Kills a change to
    the enlargement, the border, the padding or the page mode."""
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(_cohort(tmp_path, (UID, [LABEL_BOX])))
        session.load_config(_config(tmp_path))
        session.redact(show_progress=False)
    assert reader.calls == [((52, 48), "--oem 3 --psm 7")]


def test_the_pass_row_and_the_record_count_what_was_read(tmp_path, detector, reader):
    db = tmp_path / "s.db"
    with Session(str(db)) as session:
        session.ingest(_cohort(tmp_path, (UID, REGIONS)))
        session.load_config(_config(tmp_path))
        session.redact(show_progress=False)
        record = services.burned_in_text_record(_only_instance(session))
        session.generate_report(str(tmp_path / "r.md"))
    assert ("under action redact and scope all and target [dates, ids] with "
            "the db text detector read 1 of 1 in-scope instance(s) holding "
            "pixels (0 already checked, 0 could not be read; 0 out of scope, "
            "not read; 0 box(es) over a quarter of the frame not redacted) and "
            "found text on 1 (4 region(s)); 1 redacted; 1 region(s) read as "
            "dates, 1 as ids, 1 left in place, 2 unreadable redacted"
            ) in (tmp_path / "r.md").read_text()
    rows = [r for r in audit_rows(db) if r[0] == "REDACTION"]
    assert rows == [(
        "REDACTION", "burned_in_text",
        "burned_in_text (action redact, scope all, target [dates, ids], "
        "detector db, min_confidence 80): read 1 of 1 in-scope instance(s) "
        "holding pixels (0 out of scope), 0 could not be read, 0 already "
        "checked under this policy; text found on 1 (4 region(s)); 0 box(es) "
        "over a quarter of the frame not redacted; 1 redacted; 1 region(s) "
        "read as dates, 1 as ids, 1 left in place, 2 unreadable redacted. "
        "detection by PP-OCRv3 (sha256 03f550c6b406fda8) on OpenCV "
        f"{STUB_OPENCV}, regions read by tesseract 5.5.3.")]
    assert record["counts"] == {"dates": 1, "ids": 1, "left": 1, "unread": 2}
    assert record["detector"] == "db"
    assert record["tesseract"] == "5.5.3"
    policy = {"action": "redact", "scope": "all", "detector": "db",
              "min_confidence": 80, "target": ["dates", "ids"]}
    assert record["policy"] == services._burned_in_text_policy_hash(policy, [])


@pytest.mark.parametrize("target", [
    list(kinds) for n in (1, 2, 3)
    for kinds in itertools.combinations(["dates", "ids", "names"], n)])
def test_a_narrowed_target_never_boxes_what_any_would_not(
        tmp_path, detector, reader, monkeypatch, target):
    """The invariant, over every subset of the kinds: the boxes under a
    narrowed target are a subset of `any`'s, and under `any` no crop is
    read."""
    # A names model that reads the sentinel label as a person, so `names`
    # takes a region too.
    fake = FakeNlp({SENTINEL: [("PERSON", SENTINEL)]})
    monkeypatch.setattr(text_kinds, "_MODEL", None)
    monkeypatch.setattr(text_kinds, "_load_model", lambda: fake)
    monkeypatch.setattr(text_kinds, "_spacy_version", lambda: "3.8.16")
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(_cohort(tmp_path, (UID, REGIONS)))
        inst = _only_instance(session)
        anything = pixel_analysis._text_boxes(inst, 80, (), "any", "db")
        assert reader.calls == []
        narrowed = pixel_analysis._text_boxes(inst, 80, (), target, "db")
    assert len(anything.boxes) == 5
    assert set(map(tuple, narrowed.boxes)) <= set(map(tuple, anything.boxes))
    assert narrowed.counts["unread"] == 2


def test_each_region_is_classified_alone_never_joined_with_its_row(
        tmp_path, detector, reader, monkeypatch):
    """Two boxes on one row reading `DOE,` and `JANE`: a names model that
    takes only the joined `DOE, JANE` as a person finds no name, because
    each region is its own line. Kills classifying every region together,
    which `_lines_of` would group by row into a reading no crop made."""
    family, given = (4, 40, 14, 48), (20, 40, 40, 48)   # 10 and 20 wide
    detector.regions = [family, given]
    reader.readings = {10: "DOE,", 20: "JANE"}
    joined = [("PERSON", "DOE, JANE")]
    fake = FakeNlp({"DOE, JANE": joined, "Doe, Jane": [("PERSON", "Doe, Jane")]})
    monkeypatch.setattr(text_kinds, "_MODEL", None)
    monkeypatch.setattr(text_kinds, "_load_model", lambda: fake)
    monkeypatch.setattr(text_kinds, "_spacy_version", lambda: "3.8.16")
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(_cohort(tmp_path, (UID, [family, given])))
        found = pixel_analysis._text_boxes(_only_instance(session), 80, (),
                                           ["names"], "db")
    assert found.boxes == []
    assert found.counts == {"names": 0, "left": 2, "unread": 0}
    assert all(" " not in text for text in fake.seen)


def test_check_burned_in_reads_the_regions_too(tmp_path, detector, reader):
    """The export's read classifies as `redact()` does: the image holding a
    date is withheld, the one holding only a label is written."""
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(_cohort(tmp_path, (UID, [DATE_BOX]),
                               (OTHER, [LABEL_BOX])))
        session.load_config(_config(tmp_path, action="withhold", target="[dates]"))
        session.anonymize(session.audit())
        session.export(str(tmp_path / "out"), use_compression=False,
                       show_progress=False, check_burned_in=True)
    files = exported_files(tmp_path / "out")
    assert len(files) == 1
    assert _holds_ink(pixels_of(files[0])[0], LABEL_BOX)


# --- refusals ----------------------------------------------------------------

def test_a_narrowed_target_under_db_without_tesseract_is_refused_before_anything(
        tmp_path, detector, no_reader):
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(_cohort(tmp_path, (UID, REGIONS)))
        session.load_config(_config(tmp_path))
        with pytest.raises(OcrUnavailableError):
            session.redact(show_progress=False)
        with pytest.raises(OcrUnavailableError):
            session.export(str(tmp_path / "out"), use_compression=False,
                           show_progress=False, check_burned_in=True)
    assert detector.calls == 0


def test_under_any_db_needs_no_tesseract(tmp_path, detector, no_reader):
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(_cohort(tmp_path, (UID, REGIONS)))
        session.load_config(_config(tmp_path, target="any"))
        assert session.redact(show_progress=False) == 1


# --- what is never kept -------------------------------------------------------

def test_no_reading_reaches_a_row_log_store_or_report(tmp_path, detector, reader, caplog):
    caplog.set_level(logging.DEBUG)
    db = tmp_path / "s.db"
    with Session(str(db)) as session:
        session.ingest(_cohort(tmp_path, (UID, REGIONS)))
        session.load_config(_config(tmp_path))
        session.redact(show_progress=False)
        session.export(str(tmp_path / "out"), use_compression=False,
                       show_progress=False)
        session.generate_report(str(tmp_path / "r.md"))
        session.save(sync=True)
    assert SENTINEL not in repr(audit_rows(db))
    assert SENTINEL not in caplog.text
    assert SENTINEL.encode() not in db.read_bytes()
    assert SENTINEL not in (tmp_path / "r.md").read_text()


def test_a_crop_read_that_raises_quotes_no_message_and_leaves_no_record(
        tmp_path, detector, reader):
    reader.fail = RuntimeError(SENTINEL)
    db = tmp_path / "s.db"
    with Session(str(db)) as session:
        session.ingest(_cohort(tmp_path, (UID, REGIONS)))
        session.load_config(_config(tmp_path))
        session.redact(show_progress=False)
        assert services.burned_in_text_record(_only_instance(session)) is None
    rows = audit_rows(db)
    assert SENTINEL not in repr(rows)
    assert any("text detection failed on frame 0: RuntimeError" in r[2]
               for r in rows)


# --- the crop's geometry ------------------------------------------------------

def _bar(size, centre, length, height, angle):
    """A frame holding one filled rotated rectangle, and its quad."""
    rad = np.radians(angle)
    u = np.array([np.cos(rad), np.sin(rad)])
    v = np.array([-np.sin(rad), np.cos(rad)])
    c = np.array(centre, dtype=float)
    ys, xs = np.mgrid[0:size, 0:size]
    rel = np.stack([xs - c[0], ys - c[1]], axis=-1)
    inside = ((np.abs(rel @ u) <= length / 2) & (np.abs(rel @ v) <= height / 2))
    grey = np.where(inside, 255, 0).astype(np.uint8)
    quad = np.array([c - u * length / 2 - v * height / 2,
                     c + u * length / 2 - v * height / 2,
                     c + u * length / 2 + v * height / 2,
                     c - u * length / 2 + v * height / 2])
    return grey, quad


@pytest.mark.parametrize("angle", [-20, 0, 20])
def test_a_tilted_line_is_levelled_and_padded_by_a_third_of_its_height(angle):
    """The crop is 12 + 2 x 4 rows by 80 + 2 x 4 columns (pad = round(0.30
    x 12)), and the ink in it lies flat. Kills the deskew dropped or turned
    the wrong way, and a change to the padding."""
    grey, quad = _bar(200, (100, 100), 80, 12, angle)
    crop = pixel_analysis._crop_for_reading(grey, quad)
    assert crop.shape == (20, 88)
    rows = np.nonzero((crop > 128).any(axis=1))[0]
    cols = np.nonzero((crop > 128).any(axis=0))[0]
    assert rows.max() - rows.min() + 1 <= 14
    assert cols.max() - cols.min() + 1 >= 78


@pytest.mark.parametrize("quad, size, angle", [
    ([[0, 0], [40, 0], [40, 10], [0, 10]], (40, 10), 0.0),
    ([[0, 10], [0, 0], [40, 0], [40, 10]], (40, 10), 0.0),
    # A long line steeper than 45 degrees keeps its angle ...
    (_bar(200, (100, 100), 80, 12, 70)[1], (80, 12), 70.0),
    # ... a near-square glyph block at the same tilt is read upright, its
    # sides swapped, as the spike's `rect_of` does.
    (_bar(200, (100, 100), 14, 12, 70)[1], (12, 14), -20.0),
], ids=["straight", "other-corner", "steep-line", "steep-glyph"])
def test_the_rectangle_takes_the_longer_side_and_reads_a_glyph_upright(quad, size, angle):
    _centre, width, height, found = pixel_analysis._crop_rect(quad)
    assert (width, height) == pytest.approx(size)
    assert found == pytest.approx(angle, abs=1e-6)

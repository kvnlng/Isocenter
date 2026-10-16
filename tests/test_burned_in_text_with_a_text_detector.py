"""`burned_in_text: {detector: db}`: decide "is this text?" with a
text-detection model (#1121, PR C).

The owner's rulings of 2026-10-15 these tests hold:

- `detector: tesseract | db`, default `tesseract`; refused in any other
  spelling, `null` included;
- the PP-OCRv3 English DB model from opencv_zoo, fp32, Apache-2.0, shipped
  as `isocenter/resources/text_detection_en_ppocrv3_2023may.onnx`, sha256
  `03f550c6...6587`, 2,423,490 bytes, with a NOTICE beside it (Q-C2, Q-C3);
- OpenCV is the optional `textdetect` extra; where it cannot import (every
  free-threaded build) `detector: db` raises `TextDetectorUnavailableError`,
  an `OcrUnavailableError`, before anything (Q-C1);
- `min_confidence` under `db` is the DB score times 100, default 80 (Q-C5);
- under `db` with `target: any` no tesseract is needed, the two-letters
  rule does not apply, and a lone `L` or `R` the detector finds is blanked
  (Q-C6); a narrowed target's crop read is
  `test_burned_in_text_detector_reads_its_regions.py`;
- the whole frame, its long side brought to 1280 only when larger, never
  upscaled, each side rounded to 32 (design 1, after the spike);
- a box whose padded straight box, clipped to the frame, has a short side
  more than a quarter of the frame's short side is dropped and counted
  (fixed, no key; measured that way by the spike);
- the model's identity lives in the record, never the policy hash: only
  `redact()` reads an image again when it changes, and a plain `export()`
  trusts a current record with no OpenCV (the ruling made for PR B's
  `names_by`, applied here);
- nothing a detector read reaches a row, log, exception, store or hash.

**How the detector is faked.** `_detect_boxes` is the seam: the one OpenCV
call. `FakeDetector` stands in for it and finds ink (255) on the frame it
is handed, returning the ink's extent as a quad, so a blanked frame has
nothing left to find. Threads only: a patch does not cross a process. The
real model is `test_burned_in_text_with_the_real_text_detector.py`.
"""
import hashlib
import logging
import pathlib
import threading
from types import SimpleNamespace

import numpy as np
import pytest
import yaml

from isocenter import config_manager, pixel_analysis, services, text_detection
from isocenter.reporting import BurnedInTextSummary
from isocenter.session import DicomSession as Session

from test_redact_blanks_burned_in_text import (
    INK, INK_BOX, ONE, PADDED, TWO, audit_rows, exported_files, pixels_of,
    write_image)

ROWS, COLS = 64, 96
SENTINEL = "QZXJ7DETECTORSENTINEL"
STUB_OPENCV = "5.0.0-stub"


class FakeDetector:
    """`text_detection._detect_boxes` that finds ink, for threads only.

    Ink is found on the bordered image the seam is handed, so its quads are
    in that image's coordinates, which `_frame_regions` maps back."""

    def __init__(self, score=0.97):
        self.score = score
        self.calls = []
        #: Quads returned as well as the ink's, `(quad, score)`.
        self.extra = []
        self.fail = None

    def __call__(self, image, input_size, min_score):
        self.calls.append((image.shape, tuple(input_size), min_score))
        if self.fail is not None:
            raise self.fail
        assert image.dtype == np.uint8 and image.ndim == 3 and image.shape[2] == 3
        # `extra` is given in the frame's coordinates; the seam answers in
        # those of the bordered image it is handed, as OpenCV does.
        border = text_detection.DETECTION_BORDER
        found = [(np.asarray(quad, dtype=float) + border, score)
                 for quad, score in self.extra]
        ys, xs = np.nonzero(image[:, :, 0] == INK)
        if len(ys):
            x1, y1, x2, y2 = xs.min(), ys.min(), xs.max() + 1, ys.max() + 1
            found.append((np.array([[x1, y1], [x2, y1], [x2, y2], [x1, y2]],
                                   dtype=float), self.score))
        return found


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


@pytest.fixture
def detector(monkeypatch):
    """The detector stubbed at its seam, with **no tesseract at all**, so
    every test here also says `db` under `target: any` needs none."""
    fake = FakeDetector()
    monkeypatch.setattr(text_detection, "HAS_DETECTOR", True)
    monkeypatch.setattr(text_detection, "cv2",
                        SimpleNamespace(__version__=STUB_OPENCV))
    monkeypatch.setattr(text_detection, "_detector_unavailable_reason",
                        lambda: None)
    monkeypatch.setattr(text_detection, "_detect_boxes", fake)
    monkeypatch.setattr(pixel_analysis, "HAS_OCR", False)
    monkeypatch.setattr(pixel_analysis, "pytesseract", None)
    return fake


def _config(tmp_path, action, extra="", name="c.yaml", detector="db"):
    path = tmp_path / name
    path.write_text(f"privacy_profile: basic\nburned_in_text:\n  action: {action}\n"
                    f"  scope: all\n  detector: {detector}\n{extra}",
                    encoding="utf-8")
    return str(path)


def _cohort(tmp_path, *images):
    src = tmp_path / "in"
    src.mkdir(exist_ok=True)
    for i, (uid, kwargs) in enumerate(images):
        write_image(src / f"i{i}.dcm", sop_uid=uid, **kwargs)
    return str(src)


def _only_instance(session):
    return session.store.patients[0].studies[0].series[0].instances[0]


def _record(inst):
    return services.burned_in_text_record(inst)


def _quad(x1, y1, x2, y2):
    return np.array([[x1, y1], [x2, y1], [x2, y2], [x1, y2]], dtype=float)


# --- the shipped model ------------------------------------------------------

RESOURCES = pathlib.Path(text_detection.__file__).parent / "resources"
PINNED_SHA256 = "03f550c6b406fda8bf54bd8327815f6c7e2edd98cea02348c93d879254366587"


def test_the_shipped_model_is_the_pinned_file():
    """Literal, not `MODEL_SHA256`: a constant moved with the file would
    agree with it."""
    path = RESOURCES / "text_detection_en_ppocrv3_2023may.onnx"
    data = path.read_bytes()
    assert hashlib.sha256(data).hexdigest() == PINNED_SHA256
    assert len(data) == 2_423_490
    assert text_detection.MODEL_SHA256 == PINNED_SHA256
    assert text_detection.MODEL_BYTES == 2_423_490
    assert text_detection.model_path() == str(path)


def test_the_notice_names_the_file_its_sha256_size_licence_and_source():
    notice = (RESOURCES / text_detection.MODEL_NOTICE).read_text(encoding="utf-8")
    assert text_detection.MODEL_NOTICE == "text_detection_en_ppocrv3_2023may.NOTICE"
    for needle in (text_detection.MODEL_FILE, PINNED_SHA256, "2,423,490",
                   "Apache License, Version 2.0",
                   "Copyright (c) 2016 PaddlePaddle Authors",
                   "https://github.com/opencv/opencv_zoo/tree/main/models/text_detection_ppocr",
                   "https://github.com/PaddlePaddle/PaddleOCR",
                   "en_PP-OCRv3_det_infer", "arXiv:2206.03001"):
        assert needle in notice, needle


def test_a_model_that_is_not_the_pinned_file_is_unavailable(tmp_path, monkeypatch):
    if not text_detection.HAS_DETECTOR:
        monkeypatch.setattr(text_detection, "HAS_DETECTOR", True)
        monkeypatch.setattr(text_detection, "cv2", SimpleNamespace(
            __version__=STUB_OPENCV, dnn_TextDetectionModel_DB=object))
    other = tmp_path / "resources" / text_detection.MODEL_FILE
    other.parent.mkdir()
    other.write_bytes(b"not the model")
    monkeypatch.setattr(text_detection, "model_path", lambda: str(other))
    reason = text_detection._detector_unavailable_reason()
    assert reason is not None and "not the pinned file" in reason
    other.unlink()
    assert "missing" in text_detection._detector_unavailable_reason()


def test_no_opencv_is_unavailable_and_names_the_extra(monkeypatch):
    monkeypatch.setattr(text_detection, "HAS_DETECTOR", False)
    monkeypatch.setattr(text_detection, "_DETECTOR_IMPORT_ERROR", "No module named 'cv2'")
    assert "No module named 'cv2'" in text_detection._detector_unavailable_reason()
    with pytest.raises(text_detection.TextDetectorUnavailableError) as raised:
        text_detection._require_detector("redact()")
    assert isinstance(raised.value, pixel_analysis.OcrUnavailableError)
    assert 'pip install "isocenter[textdetect]"' in str(raised.value)
    assert "free-threaded" in str(raised.value)


# --- the frame: size, the box-size rule, boxes ---------------------------------

# Sizes of the frame with its 16 px border on each side, by hand: the
# bordered 2080 x 2592 brought to 1280 on its long side, 1027 rounded to
# 1024; 380 rounds to 384; the bordered 1280 x 1032 is exactly 1280.
@pytest.mark.parametrize("rows, cols, size", [
    (2048, 2560, (1280, 1024)),     # long side brought to 1280
    (348, 348, (384, 384)),         # native, rounded to 32: never 1280
    (128, 128, (160, 160)),
    (1248, 1000, (1024, 1280)),     # bordered, exactly 1280: not resized
    (5800, 2000, (448, 1280)),
])
def test_the_frame_is_read_whole_and_never_upscaled(detector, rows, cols, size):
    """The seam is handed the frame at its own size, with the model's
    input size beside it; OpenCV maps quads back to the frame. Kills an
    upscale of small frames (the spike's false PT frames) and tiling."""
    text_detection._frame_regions(np.zeros((rows, cols), np.uint8), 0, 80)
    [(shape, input_size, min_score)] = detector.calls
    assert shape == (rows + 32, cols + 32, 3)
    assert input_size == size
    # Every candidate is asked for, whatever its score, so the region cap
    # counts them all; the policy's threshold is applied after.
    assert min_score == 0.0


@pytest.mark.parametrize("height, kept", [(87, True), (88, False)])
def test_a_box_over_a_quarter_of_the_frame_is_dropped_and_counted(detector, height, kept):
    """On a 400x400 frame a padded box exactly 25% tall is kept and one
    pixel more is not. The quad sits on the top edge, so its box is
    clipped there and grows by one padding: 87 + `_padding(87)` (13) is
    100, and 88 + 13 is 101. The quad itself is under 100 both times."""
    assert pixel_analysis._padding(87) == pixel_analysis._padding(88) == 13
    detector.extra = [(_quad(10, 0, 300, height), 0.99)]
    regions, oversized = text_detection._frame_regions(
        np.zeros((400, 400), np.uint8), 0, 80)
    assert (len(regions), oversized) == ((1, 0) if kept else (0, 1))
    if kept:
        [region] = regions
        assert region.box[:2] == (0, 100)


def test_the_box_size_rule_measures_the_frames_short_side(detector):
    """A 400x1000 frame measures against 400: a padded box 126 tall is
    dropped though it is an eighth of the long side, and one 98 tall is
    kept."""
    detector.extra = [(_quad(10, 10, 300, 111), 0.99), (_quad(10, 200, 300, 276), 0.99)]
    regions, oversized = text_detection._frame_regions(
        np.zeros((400, 1000), np.uint8), 0, 80)
    assert oversized == 1
    assert [r.box[:2] for r in regions] == [(200 - 11, 276 + 11)]


def test_a_tilted_box_is_measured_by_its_padded_straight_box(detector):
    """A quad 20 px high tilted 30 degrees on a 200x300 frame: its own
    short side is 20, under the limit of 50, but its straight box is about
    67 tall before padding, so it is dropped and counted. The rule is
    measured on the padded straight box, as the spike measured it (spike
    follow-up 3: a tilted 0.88 box on a 348 px IVUS frame, quad 69 px
    against 87, padded box 107)."""
    angle = np.deg2rad(30)
    corners = np.array([[0, 0], [100.0, 0], [100.0, 20.0], [0, 20.0]])
    rot = np.array([[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]])
    quad = corners @ rot.T + [120.0, 60.0]
    assert text_detection._quad_short_side(quad) == pytest.approx(20.0)
    detector.extra = [(quad, 0.88)]
    regions, oversized = text_detection._frame_regions(
        np.zeros((200, 300), np.uint8), 0, 80)
    assert (regions, oversized) == ([], 1)


def test_the_box_size_rule_is_a_quarter():
    assert text_detection.MAX_TEXT_BOX_FRACTION == 0.25


def test_the_model_settings_are_the_ones_the_spike_measured():
    """The spike's values, not the zoo's `ppocr_det.py` (ImageNet mean
    123.675, 116.28, 103.53 with the scale divided by its std): only these
    were measured, so a change needs a new measurement."""
    assert text_detection.INPUT_MEAN == (122.67891434, 116.66876762, 104.00698793)
    assert text_detection.INPUT_SCALE == 1.0 / 255.0
    assert (text_detection.BINARY_THRESHOLD, text_detection.UNCLIP_RATIO,
            text_detection.LONG_SIDE) == (0.3, 2.0, 1280)
    # The region cap (2,000, owner ruling of 2026-10-16) and the border
    # (16, spike follow-up 2) were set after it: `test_the_cap_is_2000`
    # and `test_the_border_is_16`.


def test_the_border_is_16():
    assert text_detection.DETECTION_BORDER == 16


def test_the_seam_hands_opencv_the_spikes_settings(monkeypatch):
    """`_detect_boxes` itself, against a recording stand-in for the model:
    the polygon threshold, the input parameters (swapRB on, as the spike
    called it) and quads handed back as `(4, 2)` arrays with their scores."""
    seen = []

    class Model:
        def setPolygonThreshold(self, value):
            seen.append(("threshold", value))

        def setInputParams(self, scale, size, mean, swap):
            seen.append(("input", scale, size, mean, swap))

        def detect(self, image):
            seen.append(("detect", image.shape))
            return ([[[1, 2], [3, 2], [3, 4], [1, 4]]], [0.9])

    monkeypatch.setattr(text_detection, "_model", Model)
    image = np.zeros((40, 50, 3), np.uint8)
    [(quad, score)] = text_detection._detect_boxes(image, (64, 32), 0.8)
    assert seen == [("threshold", 0.8),
                    ("input", 1.0 / 255.0, (64, 32), text_detection.INPUT_MEAN, True),
                    ("detect", (40, 50, 3))]
    assert quad.shape == (4, 2) and quad[2].tolist() == [3.0, 4.0]
    assert score == pytest.approx(0.9)


def test_a_tilted_quad_becomes_its_padded_straight_box_clipped(detector):
    """A quad tilted about 15 degrees, 20 px high: its short side is the
    text's height, so the padding is `_padding(20)` = 3, applied to the
    quad's axis-aligned box, and clipped at the frame's edge."""
    angle = np.deg2rad(15)
    width, height = 100.0, 20.0
    corners = np.array([[0, 0], [width, 0], [width, height], [0, height]])
    rot = np.array([[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]])
    quad = corners @ rot.T + [1.0, 30.0]
    detector.extra = [(quad, 0.95)]
    # 300 rows: the padded box is 52 tall, over a quarter of 200.
    [region] = text_detection._frame_regions(np.zeros((300, 400), np.uint8), 4, 80)[0]
    assert region.frame == 4
    assert text_detection._quad_short_side(quad) == pytest.approx(20.0)
    y1 = int(np.floor(quad[:, 1].min())) - 3
    y2 = int(np.ceil(quad[:, 1].max())) + 3
    x2 = int(np.ceil(quad[:, 0].max())) + 3
    assert region.box == (y1, y2, 0, x2)       # x1 clipped at 0
    # Literal, by hand: x' = x cos - y sin + 1 peaks at 100 cos 15 + 1 =
    # 97.59, so 98 + 3; y' = x sin + y cos + 30 runs 30 to 75.20.
    assert region.box == (27, 79, 0, 101)
    assert region.score == pytest.approx(0.95)


@pytest.mark.parametrize("score, kept", [(0.80, True), (0.7999, False)])
def test_the_score_threshold_is_the_policy_number_over_100_inclusive(detector, score, kept):
    detector.extra = [(_quad(10, 10, 60, 20), score)]
    regions, _ = text_detection._frame_regions(np.zeros((100, 100), np.uint8), 0, 80)
    assert bool(regions) is kept


def test_a_frame_is_grey_uint8_in_three_channels():
    """Scaled as tesseract's frame is, a colour frame first the mean of its
    channels (as the spike read it), no polarity flip."""
    wide = np.array([[0, 1000], [500, 1000]], dtype=np.uint16)
    out = text_detection._as_bgr8(wide)
    assert out.dtype == np.uint8 and out.shape == (2, 2, 3)
    assert out[:, :, 0].tolist() == [[0, 255], [127, 255]]
    assert (out[:, :, 0] == out[:, :, 2]).all()
    colour = np.zeros((2, 2, 3), np.uint8)
    colour[0, 0] = (30, 60, 90)
    assert text_detection._as_bgr8(colour)[0, 0].tolist() == [60, 60, 60]


def test_each_thread_builds_its_own_network(monkeypatch):
    """A `cv2.dnn` network is not shared between threads (3.14t and a
    `:memory:` store run the pass in threads)."""
    built = []

    class Model:
        def __init__(self, net):
            built.append((net, threading.get_ident()))

        def __getattr__(self, name):
            return lambda *args: None

    fake = SimpleNamespace(setNumThreads=lambda n: None,
                           dnn=SimpleNamespace(readNet=lambda path: object()),
                           dnn_TextDetectionModel_DB=Model)
    monkeypatch.setattr(text_detection, "cv2", fake)
    # A fresh one of the module's own type, so a mutant that made it a
    # plain shared object is still what this test exercises.
    monkeypatch.setattr(text_detection, "_LOCAL", type(text_detection._LOCAL)())
    models = []
    for _ in range(2):
        worker = threading.Thread(target=lambda: models.append(
            (text_detection._model(), text_detection._model())))
        worker.start()
        worker.join()
    assert len(built) == 2
    assert models[0][0] is models[0][1]
    assert models[0][0] is not models[1][0]


# --- configuration --------------------------------------------------------------

@pytest.mark.parametrize("value", ["DB", "east", "null", "Tesseract"])
def test_a_detector_not_named_exactly_is_refused(tmp_path, value):
    path = tmp_path / "c.yaml"
    path.write_text("burned_in_text: {action: redact, scope: all, "
                    f"detector: {value}}}\n", encoding="utf-8")
    shown = None if value == "null" else value
    with pytest.raises(ValueError) as raised:
        config_manager.ConfigLoader.load_unified_config(str(path))
    assert str(raised.value) == (
        f"{path}: 'burned_in_text' detector {shown!r} is not one of "
        "tesseract, db (exact spelling)")


@pytest.mark.parametrize("text, expected", [
    ("{action: redact, scope: all}", {"detector": "tesseract", "min_confidence": 80}),
    ("{action: redact, scope: all, detector: db}", {"detector": "db", "min_confidence": 80}),
    ("{action: redact, scope: all, detector: db, min_confidence: 65}",
     {"detector": "db", "min_confidence": 65}),
])
def test_the_detector_and_its_default_threshold_are_filled_in(tmp_path, text, expected):
    path = tmp_path / "c.yaml"
    path.write_text(f"burned_in_text: {text}\n", encoding="utf-8")
    policy = config_manager.ConfigLoader.load_unified_config(str(path))[5]
    assert policy == {"action": "redact", "scope": "all", "target": "any",
                      **expected}


def test_save_round_trips_the_detector(tmp_path, monkeypatch):
    """A file is portable: it loads and saves with no OpenCV."""
    monkeypatch.setattr(text_detection, "HAS_DETECTOR", False)
    with Session(str(tmp_path / "s.db")) as session:
        session.load_config(_config(tmp_path, "withhold"))
        session.configuration.config_path = str(tmp_path / "saved.yaml")
        session.configuration.save()
    saved = yaml.safe_load((tmp_path / "saved.yaml").read_text())["burned_in_text"]
    assert saved == {"action": "withhold", "scope": "all", "detector": "db",
                     "min_confidence": 80, "target": "any"}


# --- the pass ----------------------------------------------------------------

def test_redact_under_db_blanks_the_text_with_no_tesseract(tmp_path, detector):
    """Kills routing `db` through the OCR arm (`HAS_OCR` False here) and a
    `_require_ocr` left on the `db` path."""
    db = tmp_path / "s.db"
    with Session(str(db)) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)]})))
        session.load_config(_config(tmp_path, "redact"))
        assert session.redact(show_progress=False) == 1
        inst = _only_instance(session)
        record = _record(inst)
        assert record["boxes"] == [[0, *PADDED]]
        out = tmp_path / "out"
        session.export(str(out), use_compression=False, show_progress=False)
    [path] = exported_files(out)
    y1, y2, x1, x2 = PADDED
    assert not pixels_of(path)[0, y1:y2, x1:x2].any()
    assert detector.calls


def test_a_lone_side_marker_is_blanked_under_db(tmp_path, detector):
    """Q-C6 (a): there is no reading, so no two-letters rule; a single
    glyph the detector finds is a box. Under tesseract one letter is not."""
    marker = (0, 40, 48, 70, 76)
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [marker]})))
        session.load_config(_config(tmp_path, "redact"))
        session.redact(show_progress=False)
        assert len(_record(_only_instance(session))["boxes"]) == 1
        assert not (_only_instance(session).get_pixel_data() == INK).any()


def test_a_box_taller_than_a_quarter_of_the_frame_is_left_and_counted(tmp_path, detector):
    """One 45%-tall high-score box beside a small one: the small one is
    blanked, the tall one left whole, and the row and section 5 count it.
    Red if the rule is removed (the tall box is blanked) or its
    comparison flipped (the small one is left)."""
    db = tmp_path / "s.db"
    with Session(str(db)) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)]})))
        session.load_config(_config(tmp_path, "redact"))
        # The tall box comes from the detector beside the ink: 30 px high
        # and 40 wide, so its short side (30) is 47% of the frame's 64.
        detector.extra = [(_quad(50, 30, 90, 60), 0.99)]
        before = _only_instance(session).get_pixel_data().copy()
        session.redact(show_progress=False)
        after = _only_instance(session).get_pixel_data()
        y1, y2, x1, x2 = PADDED
        assert not after[y1:y2, x1:x2].any()
        outside = np.ones(after.shape, bool)
        outside[y1:y2, x1:x2] = False
        assert (after[outside] == before[outside]).all()
        assert _record(_only_instance(session))["boxes"] == [[0, *PADDED]]
        session.generate_report(str(tmp_path / "r.md"))
    [row] = [r for r in audit_rows(db) if r[0] == "REDACTION"]
    assert "; 1 box(es) over a quarter of the frame not redacted;" in row[2]
    report = (tmp_path / "r.md").read_text()
    assert "1 box(es) over a quarter of the frame not redacted" in report


def test_the_pass_row_names_the_detector_and_the_model(tmp_path, detector):
    db = tmp_path / "s.db"
    with Session(str(db)) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)]}),
                               (TWO, {})))
        session.load_config(_config(tmp_path, "redact"))
        session.redact(show_progress=False)
    rows = [r for r in audit_rows(db) if r[0] == "REDACTION"]
    assert rows == [(
        "REDACTION", "burned_in_text",
        "burned_in_text (action redact, scope all, target any, detector db, "
        "min_confidence 80): read 2 of 2 in-scope instance(s) holding pixels "
        "(0 out of scope), 0 could not be read, 0 already checked under this "
        "policy; text found on 1 (1 region(s)); 0 box(es) over a quarter of "
        "the frame not redacted; 1 redacted. detection by PP-OCRv3 (sha256 "
        f"03f550c6b406fda8) on OpenCV {STUB_OPENCV}.")]


def test_the_record_holds_the_detector_build_and_no_tesseract(tmp_path, detector):
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)]})))
        session.load_config(_config(tmp_path, "withhold"))
        session.redact(show_progress=False)
        record = _record(_only_instance(session))
    assert record["detector"] == "db"
    assert record["model"] == "03f550c6b406fda8"
    assert record["opencv"] == STUB_OPENCV
    assert "tesseract" not in record
    policy = {"action": "withhold", "scope": "all", "detector": "db",
              "min_confidence": 80}
    assert record["policy"] == services._burned_in_text_policy_hash(policy, [])


def test_a_record_read_by_another_opencv_is_read_again_by_redact(tmp_path, detector, monkeypatch):
    """The build is in the record, not the hash: `redact()` reads again
    when it changed. Kills the comparison dropped."""
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)]})))
        session.load_config(_config(tmp_path, "withhold"))
        session.redact(show_progress=False)
        first = len(detector.calls)
        session.redact(show_progress=False)
        assert len(detector.calls) == first          # current: not read again
        monkeypatch.setattr(text_detection, "cv2", SimpleNamespace(__version__="5.1.0"))
        session.redact(show_progress=False)
        assert len(detector.calls) == first + 1
        assert _record(_only_instance(session))["opencv"] == "5.1.0"


def test_a_record_read_by_another_model_is_read_again_by_redact(tmp_path, detector, monkeypatch):
    """The model's sha256 is in the comparison as well as OpenCV's
    version. Kills it left out (review of #1123, R13)."""
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)]})))
        session.load_config(_config(tmp_path, "withhold"))
        session.redact(show_progress=False)
        first = len(detector.calls)
        monkeypatch.setattr(text_detection, "MODEL_SHA256", "f" * 64)
        session.redact(show_progress=False)
        assert len(detector.calls) == first + 1
        assert _record(_only_instance(session))["model"] == "f" * 16


def test_plain_export_trusts_a_current_record_with_no_opencv(tmp_path, detector, monkeypatch):
    """Owner ruling: a plain `export()` reads records, never pixels, and
    must not need the detector's packages to trust a current one. Kills
    the build put in the policy hash."""
    db = tmp_path / "s.db"
    with Session(str(db)) as session:
        session.ingest(_cohort(tmp_path, (ONE, {}), (TWO, {"ink": [(0, *INK_BOX)]})))
        session.load_config(_config(tmp_path, "withhold"))
        session.redact(show_progress=False)
        monkeypatch.setattr(text_detection, "HAS_DETECTOR", False)
        monkeypatch.setattr(text_detection, "cv2", None)
        out = tmp_path / "out"
        summary = session.export(str(out), use_compression=False, show_progress=False)
    assert ONE in summary.written_uids
    assert TWO not in summary.written_uids
    rows = audit_rows(db)
    assert not [r for r in rows if "not checked" in r[2]]
    assert any(f"withheld instance {TWO}: burned_in_text found 1" in r[2] for r in rows)


def test_redact_refuses_before_anything_when_the_detector_cannot_run(tmp_path, monkeypatch):
    """With a zone too: one pass, all or nothing, and tesseract present
    does not stand in for the detector."""
    monkeypatch.setattr(text_detection, "HAS_DETECTOR", False)
    zone = "machines:\n  - serial_number: SN-1\n    redaction_zones: [[0, 4, 0, 4]]\n"
    db = tmp_path / "s.db"
    with Session(str(db)) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)], "serial": "SN-1"})))
        session.load_config(_config(tmp_path, "redact", zone))
        before = _only_instance(session).get_pixel_data().copy()
        with pytest.raises(text_detection.TextDetectorUnavailableError, match=r"redact\(\)"):
            session.redact(show_progress=False)
        inst = _only_instance(session)
        assert inst.sop_instance_uid == ONE
        assert "_ISOCENTER_REDACTION_HASH" not in inst.attributes
        assert (inst.get_pixel_data() == before).all()
    assert not [r for r in audit_rows(db) if r[0] == "REDACTION"]


def test_export_check_refuses_before_the_scan_when_the_detector_cannot_run(tmp_path, monkeypatch):
    monkeypatch.setattr(text_detection, "HAS_DETECTOR", False)
    db = tmp_path / "s.db"
    with Session(str(db)) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)]})))
        session.load_config(_config(tmp_path, "withhold"))
        status = _only_instance(session).phi_status
        with pytest.raises(text_detection.TextDetectorUnavailableError,
                           match=r"export\(check_burned_in=True\)"):
            session.export(str(tmp_path / "out"), check_burned_in=True,
                           show_progress=False)
        assert _only_instance(session).phi_status == status
    assert not (tmp_path / "out").exists() or not exported_files(tmp_path / "out")


def test_export_check_reads_with_the_detector(tmp_path, detector):
    """Anonymized first, so the tag scan withholds nothing."""
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)]}),
                               (TWO, {"series_uid": "1.2.826.0.1.3680043.8.498.1108.9"})))
        session.load_config(_config(tmp_path, "withhold"))
        session.anonymize()
        out = tmp_path / "out"
        summary = session.export(str(out), use_compression=False,
                                 check_burned_in=True, show_progress=False)
        [run] = session._burned_in_text_passes
    assert summary.written == 1
    assert run.detector == "db" and run.with_text == 1 and run.read == 2
    assert len(detector.calls) == 2


def test_the_default_threshold_under_db_is_80(tmp_path, detector):
    detector.score = 0.79
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)]})))
        session.load_config(_config(tmp_path, "withhold"))
        session.redact(show_progress=False)
        assert _record(_only_instance(session))["boxes"] == []
    assert detector.calls[0][2] == 0.0


def test_a_detector_failure_quotes_no_message(tmp_path, detector, caplog):
    """A frame failure names the frame and the class, never the message,
    in the row, the log and the report; and the instance has no record,
    so the export withholds it."""
    caplog.set_level(logging.DEBUG)
    detector.fail = RuntimeError(f"cv2 error over {SENTINEL}")
    db = tmp_path / "s.db"
    with Session(str(db)) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)]})))
        session.load_config(_config(tmp_path, "withhold"))
        session.redact(show_progress=False)
        assert _record(_only_instance(session)) is None
        summary = session.export(str(tmp_path / "out"), use_compression=False,
                                 show_progress=False)
        session.generate_report(str(tmp_path / "r.md"))
        session.save(sync=True)
    assert ONE not in summary.written_uids
    rows = audit_rows(db)
    assert any("text detection failed on frame 0: RuntimeError" in r[2] for r in rows)
    assert not any(SENTINEL in str(cell) for row in rows for cell in row)
    assert SENTINEL not in caplog.text
    assert SENTINEL not in (tmp_path / "r.md").read_text()


def test_a_summary_built_before_the_detector_renders_as_before():
    """Tier 2: a hand-built `BurnedInTextSummary` keeps working."""
    summary = BurnedInTextSummary(source="redact()", action="report", attempted=1,
                                  read=1, unread=0, skipped=0, with_text=0,
                                  regions=0, acted=0)
    assert summary.detector == "" and summary.oversized == 0


def test_the_require_variable_is_documented():
    """CI's guard against a silent skip of the real model (#44's shape)."""
    registry = (pathlib.Path(__file__).resolve().parent.parent / "docs"
                / "environment.md").read_text(encoding="utf-8")
    assert "| **`ISOCENTER_REQUIRE_TEXT_DETECTOR`** |" in registry


# --- the region cap (review of #1123, F1; owner ruling of 2026-10-16) ------

def _tiny(i):
    """A small region inside the 64 x 96 test frame, one per index."""
    x, y = 2 + (i % 40), 2 + (i // 40) % 40
    return (_quad(x, y, x + 3, y + 3), 0.97)


def test_a_frame_denser_than_the_region_cap_is_withheld(tmp_path, detector):
    """A frame on which the detector returns `MAX_TEXT_REGIONS` regions is
    denser than the limit: it is a frame failure, so the image gets
    no record and the export withholds it. The row says why as counts."""
    cap = text_detection.MAX_TEXT_REGIONS
    detector.extra = [_tiny(i) for i in range(cap - 1)]   # + the ink: cap
    db = tmp_path / "s.db"
    with Session(str(db)) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)]})))
        session.load_config(_config(tmp_path, "redact"))
        assert session.redact(show_progress=False) == 0
        assert _record(_only_instance(session)) is None
        summary = session.export(str(tmp_path / "out"), use_compression=False,
                                 show_progress=False)
    assert ONE not in summary.written_uids
    assert exported_files(tmp_path / "out") == []
    assert any(f"text detection failed on frame 0: {cap} text regions, the "
               f"detector's limit of {cap} per frame" in r[2]
               for r in audit_rows(db))


def test_one_region_under_the_cap_is_redacted_as_usual(tmp_path, detector):
    cap = text_detection.MAX_TEXT_REGIONS
    detector.extra = [_tiny(i) for i in range(cap - 2)]   # + the ink: cap - 1
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"ink": [(0, *INK_BOX)]})))
        session.load_config(_config(tmp_path, "redact"))
        assert session.redact(show_progress=False) == 1
        session.export(str(tmp_path / "out"), use_compression=False,
                       show_progress=False)
    [path] = exported_files(tmp_path / "out")
    y1, y2, x1, x2 = PADDED
    assert not pixels_of(path)[0, y1:y2, x1:x2].any()


def test_regions_below_the_threshold_count_toward_the_cap(detector):
    """The limit counts every candidate the detector returned, not only
    those the policy keeps: a frame that dense fails however strong its
    regions are."""
    cap = text_detection.MAX_TEXT_REGIONS
    detector.extra = [(_quad(2, 2, 5, 5), 0.10)] * cap
    with pytest.raises(text_detection.TooManyTextRegions) as raised:
        text_detection._frame_regions(np.zeros((64, 96), np.uint8), 0, 80)
    assert str(raised.value) == (f"{cap} text regions, the detector's limit "
                                 f"of {cap} per frame")


def test_the_limit_is_2000_and_opencv_is_never_capped(monkeypatch):
    """Owner ruling: 2,000 regions per frame, failing closed at it. OpenCV
    is handed 0 (no limit in OpenCV 5.0.0), because it truncates before it
    drops small contours, so a cap there hides a cut under a count below
    it. Kills OpenCV handed 2000 (or 200) again."""
    assert text_detection.MAX_TEXT_REGIONS == 2000
    assert text_detection.OPENCV_MAX_CANDIDATES == 0
    seen = []

    class Model:
        def __init__(self, net):
            seen.append(("net", net))

        def __getattr__(self, name):
            return lambda *args: seen.append((name, *args))

    monkeypatch.setattr(text_detection, "cv2", SimpleNamespace(
        setNumThreads=lambda n: seen.append(("threads", n)),
        dnn=SimpleNamespace(readNet=lambda path: "net"),
        dnn_TextDetectionModel_DB=Model))
    monkeypatch.setattr(text_detection, "_LOCAL", type(text_detection._LOCAL)())
    text_detection._model()
    assert ("setMaxCandidates", 0) in seen
    assert ("threads", 1) in seen


# --- multi-frame (review of #1123, F2) ---------------------------------------

def test_a_box_on_the_third_frame_is_recorded_and_blanked_there(tmp_path, detector):
    """Kills the box's frame forced to 0 and the reader always handed frame
    0 (review R1, R2)."""
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(_cohort(tmp_path, (ONE, {"frames": 3, "ink": [(2, *INK_BOX)]})))
        session.load_config(_config(tmp_path, "redact"))
        assert session.redact(show_progress=False) == 1
        assert _record(_only_instance(session))["boxes"] == [[2, *PADDED]]
        session.export(str(tmp_path / "out"), use_compression=False,
                       show_progress=False)
    [path] = exported_files(tmp_path / "out")
    frames = pixels_of(path)
    y1, y2, x1, x2 = PADDED
    assert frames.shape[0] == 3
    assert not frames[2, y1:y2, x1:x2].any()
    assert not (frames[:2] == INK).any()
    assert (frames[:2, y1:y2, x1:x2] != 0).all()


# --- clipping (review of #1123, F5) ------------------------------------------

def test_a_quad_past_the_top_left_corner_is_clipped_to_the_frame(detector):
    """Kills the lower clip on `y1` dropped (review R5)."""
    detector.extra = [(_quad(-5, -7, 10, 10), 0.97)]
    [region], _ = text_detection._frame_regions(np.zeros((100, 100), np.uint8), 0, 80)
    y1, y2, x1, x2 = region.box
    assert (y1, x1) == (0, 0)
    assert y2 > 10 and x2 > 10


# --- the border (review of #1123, F4; spike follow-up 2) ---------------------

def test_the_frame_is_read_inside_a_border_of_zero_and_mapped_back(detector, monkeypatch):
    """The seam sees the frame inside 16 px of 0; a quad it returns in that
    image's coordinates is moved back by 16 before the box is made. Kills
    the border not added and the border not subtracted."""
    seen = []

    def seam(image, input_size, min_score):
        seen.append(image.copy())
        # The ink at (10..20, 30..40) of the frame is at (26..36, 46..56).
        return [(_quad(46, 26, 56, 36), 0.97)]

    monkeypatch.setattr(text_detection, "_detect_boxes", seam)
    frame = np.full((64, 96), 7, np.uint8)
    frame[0, 0] = 0
    frame[10:20, 30:40] = 255
    [region], _ = text_detection._frame_regions(frame, 0, 80)
    [image] = seen
    assert image.shape == (96, 128, 3)
    assert not image[:16].any() and not image[-16:].any()
    assert not image[:, :16].any() and not image[:, -16:].any()
    assert (image[16:-16, 16:-16, 0] == text_detection._as_bgr8(frame)[:, :, 0]).all()
    pad = pixel_analysis._padding(10)
    assert region.box == (10 - pad, 20 + pad, 30 - pad, 40 + pad)
    assert region.quad[0] == (30.0, 10.0)


def test_a_box_in_the_border_is_clipped_to_the_frame(detector, monkeypatch):
    """A quad reaching into the border maps to coordinates below 0, and the
    box is clipped at the frame's edge."""
    monkeypatch.setattr(text_detection, "_detect_boxes",
                        lambda image, size, score: [(_quad(10, 10, 40, 24), 0.97)])
    [region], _ = text_detection._frame_regions(np.zeros((64, 96), np.uint8), 0, 80)
    y1, y2, x1, x2 = region.box
    # The quad maps to (-6, -6)..(24, 8): 14 tall.
    assert (y1, x1) == (0, 0)
    assert (y2, x2) == (8 + pixel_analysis._padding(14), 24 + pixel_analysis._padding(14))


def test_a_box_in_the_far_border_is_clipped_to_the_frame(detector, monkeypatch):
    """The right and bottom edges clip to the frame's 96 x 64, not the
    bordered 128 x 96 (re-review of #1123, N12)."""
    monkeypatch.setattr(text_detection, "_detect_boxes",
                        lambda image, size, score: [(_quad(100, 70, 125, 84), 0.97)])
    [region], _ = text_detection._frame_regions(np.zeros((64, 96), np.uint8), 0, 80)
    y1, y2, x1, x2 = region.box
    # The quad maps to (84, 54)..(109, 68): 14 tall, past both far edges.
    assert (y2, x2) == (64, 96)
    assert (y1, x1) == (54 - pixel_analysis._padding(14), 84 - pixel_analysis._padding(14))


def test_a_quad_wholly_inside_the_border_yields_no_box(detector, monkeypatch):
    """A quad the detector puts in the border alone maps to (-14, -14)..(-6,
    -6), and its padded box clipped to the frame is empty: no region, not
    counted as oversized, and no error (review of #1123, P6). Kills the
    empty-box guard removed, which records an inverted box."""
    monkeypatch.setattr(text_detection, "_detect_boxes",
                        lambda image, size, score: [(_quad(2, 2, 10, 10), 0.97)])
    assert text_detection._frame_regions(np.zeros((64, 96), np.uint8), 0, 80) == ([], 0)

"""`burned_in_text: {detector: db}` with the real model and OpenCV (#1121).

The shipped PP-OCRv3 DB model, run by OpenCV, on pydicom's bundled
`RG3_UNCR.dcm` (the #1108 acceptance CR) with initials `JT` drawn tilted
15 degrees on its unexposed border, the shape of a lead side marker. The
detector finds them; tesseract at 80 does not (measured when this was
written: no region at 80; not asserted, since a tesseract on another
system does find one). A text-free bundled CT gives no box at the
default threshold. Through `redact()`, the redacted export carries no
ink there, in threads and in spawned processes (a spawned worker loads
the model itself).

**Skipped where the detector cannot run**, by the probe `redact()`
refuses on (`_detector_unavailable_reason`): the `textdetect` extra is
one a documented environment may lack, and the free-threaded build can
never install it (OpenCV publishes no free-threaded wheel). **Except**
under `ISOCENTER_REQUIRE_TEXT_DETECTOR=1`, which `tests.yml` sets on
every job that is not free-threaded: there the skip is a failure, so a
CI install that lost OpenCV cannot pass as a skip (#44's shape; owner
ruling Q-C4 (a)).
"""
import glob
import os

import numpy as np
import pydicom
import pytest
from PIL import Image, ImageDraw, ImageFont
from pydicom.data import get_testdata_file
from pydicom.pixels import apply_voi_lut

from isocenter import pixel_analysis, services, text_detection
from isocenter.session import DicomSession as Session

REQUIRED = os.environ.get("ISOCENTER_REQUIRE_TEXT_DETECTOR") == "1"


def _unavailable():
    return text_detection._detector_unavailable_reason()  # pylint: disable=protected-access


def test_the_detector_is_installed_where_ci_requires_it():
    """Fails, never skips, when the variable says the detector must run."""
    if not REQUIRED:
        pytest.skip("ISOCENTER_REQUIRE_TEXT_DETECTOR is not 1: the 'textdetect' "
                    "extra (opencv-python-headless) is optional here")
    assert _unavailable() is None, (
        "ISOCENTER_REQUIRE_TEXT_DETECTOR=1 and the text detector cannot run: "
        f"{_unavailable()}. Install the 'textdetect' extra.")


@pytest.fixture(autouse=True)
def _needs_the_detector(request):
    if request.node.name == "test_the_detector_is_installed_where_ci_requires_it":
        return
    reason = _unavailable()
    if reason is None:
        return
    if REQUIRED:
        pytest.fail(f"ISOCENTER_REQUIRE_TEXT_DETECTOR=1 and the detector "
                    f"cannot run: {reason}")
    pytest.skip("Requires the 'textdetect' extra (opencv-python-headless), "
                f"which has no free-threaded wheel: {reason}")


def _marked_cr(folder=None):
    """The acceptance CR with `JT` drawn tilted 15 degrees at (60, 60):
    returns the dataset and the ink mask."""
    ds = pydicom.dcmread(get_testdata_file("RG3_UNCR.dcm"))
    arr = ds.pixel_array.copy()
    canvas = Image.new("L", (300, 120), 0)
    # Pillow's own font, at a size: the same image on every runner.
    ImageDraw.Draw(canvas).text((20, 30), "JT", fill=255,
                                font=ImageFont.load_default(size=40))
    canvas = canvas.rotate(15, expand=True, resample=Image.NEAREST)
    small = np.array(canvas) > 127
    ink = np.zeros(arr.shape, bool)
    ink[60:60 + small.shape[0], 60:60 + small.shape[1]] = small
    arr[ink] = arr.max()
    ds.PixelData = arr.tobytes()
    if folder is not None:
        os.makedirs(folder, exist_ok=True)
        ds.save_as(os.path.join(folder, "cr.dcm"))
    return ds, ink


def _covers(box, ink):
    y1, y2, x1, x2 = box
    ys, xs = np.nonzero(ink)
    return (y1 <= ys.min() and ys.max() < y2 and x1 <= xs.min() and xs.max() < x2)


def test_db_finds_tilted_initials():
    # What tesseract makes of the same ink is not asserted: at 80 it found
    # nothing with tesseract 5.5 on macOS and a region on the ubuntu
    # runner (dispatch 38807015467), so it pins an installation, not ours.
    ds, ink = _marked_cr()
    arr = ds.pixel_array
    regions, oversized = text_detection._frame_regions(arr, 0, 80)
    assert any(_covers(region.box, ink) for region in regions), regions
    assert oversized == 0


@pytest.mark.parametrize("name", ["CT_small.dcm", "MR_small.dcm"])
def test_a_text_free_image_gives_no_box_at_the_default(name):
    ds = pydicom.dcmread(get_testdata_file(name))
    assert text_detection._frame_regions(
        apply_voi_lut(ds.pixel_array, ds), 0, 80) == ([], 0)


@pytest.mark.parametrize("mode", ["threads", "processes"])
def test_redact_blanks_the_initials_and_the_export_carries_none(tmp_path, monkeypatch, mode):
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    if mode == "threads":
        monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
        monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    else:
        monkeypatch.delenv("ISOCENTER_FORCE_THREADS", raising=False)
        monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")
    src = tmp_path / "in"
    _, ink = _marked_cr(str(src))
    cfg = tmp_path / "c.yaml"
    cfg.write_text("privacy_profile: basic\nburned_in_text:\n  action: redact\n"
                   "  scope: modalities\n  modalities: [CR]\n  detector: db\n",
                   encoding="utf-8")
    out = tmp_path / "out"
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(src))
        session.load_config(str(cfg))
        assert session.redact(show_progress=False) == 1
        session.export(str(out), use_compression=False, show_progress=False)
    [path] = glob.glob(str(out / "**" / "*.dcm"), recursive=True)
    written = pydicom.dcmread(path).pixel_array
    assert not written[ink].any()


@pytest.mark.parametrize("mode", ["threads", "processes"])
def test_a_narrowed_target_reads_the_region_with_the_real_tesseract(tmp_path, monkeypatch, mode):
    """The crop read end to end: the real detector, the real pytesseract,
    the worker pickled to a spawned process. What a given tesseract makes
    of `JT` differs between builds, so only what holds either way is
    asserted: no date or ID is read in it, and the region is counted as
    unreadable (redacted) or left. Skipped without tesseract: the
    variable requires the detector, not OCR."""
    if pixel_analysis._ocr_unavailable_reason() is not None:  # pylint: disable=protected-access
        pytest.skip("tesseract is not installed")
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    if mode == "threads":
        monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
        monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    else:
        monkeypatch.delenv("ISOCENTER_FORCE_THREADS", raising=False)
        monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")
    src = tmp_path / "in"
    _marked_cr(str(src))
    cfg = tmp_path / "c.yaml"
    cfg.write_text("privacy_profile: basic\nburned_in_text:\n  action: redact\n"
                   "  scope: modalities\n  modalities: [CR]\n  detector: db\n"
                   "  target: [dates, ids]\n", encoding="utf-8")
    out = tmp_path / "out"
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(src))
        session.load_config(str(cfg))
        redacted = session.redact(show_progress=False)
        inst = session.store.patients[0].studies[0].series[0].instances[0]
        record = services.burned_in_text_record(inst)
        session.export(str(out), use_compression=False, show_progress=False)
    assert record["tesseract"]
    counts = record["counts"]
    assert counts["dates"] == counts["ids"] == 0
    assert counts["unread"] + counts["left"] >= 1
    # Redacted only when a region could not be read (Q-B10 (a)).
    assert redacted == (1 if counts["unread"] else 0)
    assert len(glob.glob(str(out / "**" / "*.dcm"), recursive=True)) == 1


def _edge_text(rows, cols, corner, size):
    """A line of synthetic text touching the frame's edge at `corner`, on
    noise, and its ink (adapted from the review of #1123's corners.py)."""
    layer = Image.new("L", (cols, rows), 0)
    draw = ImageDraw.Draw(layer)
    font = ImageFont.load_default(size=size)
    left, top, right, bottom = draw.textbbox((0, 0), "SMITH JOHN 12345", font=font)
    x = 0 if corner[0] == "l" else cols - (right - left)
    y = 0 if corner[1] == "t" else rows - (bottom - top)
    draw.text((x - left, y - top), "SMITH JOHN 12345", fill=255, font=font)
    ink = np.array(layer) > 127
    arr = np.random.default_rng(1).normal(60, 15, (rows, cols)).clip(0, 255).astype(np.uint8)
    arr[ink] = 240
    return arr, ink


@pytest.mark.parametrize("rows, cols, size", [(512, 512, 13), (480, 640, 9)])
@pytest.mark.parametrize("corner", ["lt", "rt", "lb", "rb"])
def test_text_touching_the_frame_edge_is_covered_whole(rows, cols, size, corner):
    """With the 16 px border every pixel of the line is inside a box.
    Without it, when this was written, these lines were not covered whole
    (most were not found at all)."""
    arr, ink = _edge_text(rows, cols, corner, size)
    regions, _ = text_detection._frame_regions(arr, 0, 80)
    covered = np.zeros(ink.shape, bool)
    for region in regions:
        y1, y2, x1, x2 = region.box
        covered[y1:y2, x1:x2] = True
    assert not (ink & ~covered).any()

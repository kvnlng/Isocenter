"""The `burned_in_text` policy with the real tesseract binary (#1108).

The issue's acceptance case: pydicom's bundled `RG3_UNCR.dcm` (a Fuji CR,
MONOCHROME1, no Device Serial Number) with a name and a date drawn into
its pixels at the stored maximum, on the unexposed border. On f31758d3
every ink pixel reached the export under `export()` and under
`export(check_burned_in=True)`, and the report graded PASS. With only
`burned_in_text: {action: redact}` configured (no machine rule, no zone),
`redact()` must blank it and the export must carry no ink there; under
`report` the pixels are written with rows; under `withhold` the instance
is not written and is reported.

The real binary, under threads and under processes: a spawned worker
must run the caller's tesseract and hand back boxes, and the redacted UID
is still derived in the parent. And determinism: two stores under one
fixed secret give the same boxes, the same UID and the same exported
bytes, on whichever interpreter runs this (the free-threaded build runs
the pass in threads).

Skipped where OCR cannot run, by the probe `redact()` itself refuses on
(`_ocr_unavailable_reason`), as `test_discovery_integration.py` skips:
the `ocr` extra and the tesseract binary are a documented environment's
to lack.
"""
import glob
import os
import sqlite3

import numpy as np
import pydicom
import pytest
from PIL import Image, ImageDraw, ImageFont
from pydicom.data import get_testdata_file

from isocenter import pixel_analysis
from isocenter.session import DicomSession as Session

from support.project_secret import load_fixed_secret

NAME = "DOE, JANE  1970-01-01"


@pytest.fixture(autouse=True)
def _needs_ocr():
    reason = pixel_analysis._ocr_unavailable_reason()  # pylint: disable=protected-access
    if reason is not None:
        pytest.skip("Requires the 'ocr' extra (pytesseract) and a tesseract "
                    f"binary: this test reads burned-in pixel text. {reason}")


def _burned_cr(folder):
    """The acceptance image: returns the ink mask and the ink's value."""
    os.makedirs(folder, exist_ok=True)
    ds = pydicom.dcmread(get_testdata_file("RG3_UNCR.dcm"))
    assert "DeviceSerialNumber" not in ds
    arr = ds.pixel_array.copy()
    canvas = Image.new("L", (arr.shape[1], arr.shape[0]), 0)
    # Pillow's own font, at a size: present wherever Pillow has FreeType,
    # so the image is the same on every runner.
    ImageDraw.Draw(canvas).text((60, 60), NAME, fill=255,
                                font=ImageFont.load_default(size=28))
    ink = np.array(canvas) > 127
    value = int(arr.max())
    arr[ink] = value
    ds.PixelData = arr.tobytes()
    ds.save_as(os.path.join(folder, "cr.dcm"))
    return ink, value


def _run(tmp_path, action, name="run", secret=False):
    src = tmp_path / f"{name}-in"
    ink, value = _burned_cr(str(src))
    cfg = tmp_path / f"{name}.yaml"
    cfg.write_text(f"privacy_profile: basic\nburned_in_text:\n  action: {action}\n",
                   encoding="utf-8")
    db = tmp_path / f"{name}.db"
    out = tmp_path / f"{name}-out"
    with Session(str(db)) as session:
        if secret:
            load_fixed_secret(session)
        session.ingest(str(src))
        session.load_config(str(cfg))
        redacted = session.redact(show_progress=False)
        inst = session.store.patients[0].studies[0].series[0].instances[0]
        record = inst.attributes.get("_ISOCENTER_BURNED_IN_TEXT")
        uid = inst.sop_instance_uid
        session.export(str(out), use_compression=False, show_progress=False)
    files = glob.glob(os.path.join(str(out), "**", "*.dcm"), recursive=True)
    with sqlite3.connect(str(db)) as conn:
        rows = conn.execute("SELECT action_type, entity_uid, details "
                            "FROM audit_log").fetchall()
    return {"ink": ink, "value": value, "redacted": redacted, "record": record,
            "uid": uid, "files": files, "rows": rows}


@pytest.mark.parametrize("executor", ["threads", "processes"])
def test_redact_blanks_the_name_on_a_cr_with_no_serial(tmp_path, monkeypatch, executor):
    monkeypatch.delenv("ISOCENTER_FORCE_THREADS", raising=False)
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS" if executor == "threads"
                       else "ISOCENTER_FORCE_PROCESSES", "1")
    run = _run(tmp_path, "redact")
    assert run["redacted"] == 1
    [path] = run["files"]
    out = pydicom.dcmread(path)
    left = int((out.pixel_array[run["ink"]] == run["value"]).sum())
    assert left == 0, f"{left} of {int(run['ink'].sum())} ink pixels exported"
    assert out.SOPInstanceUID == run["uid"]
    assert out.ImageType[0] == "DERIVED"
    # What tesseract read never reaches a row, whatever it read.
    assert "JANE" not in str(run["rows"]).upper()
    assert "1970-01-01" not in str(run["rows"])


def test_report_writes_the_pixels_and_a_row(tmp_path):
    run = _run(tmp_path, "report")
    assert run["redacted"] == 0
    [path] = run["files"]
    assert (pydicom.dcmread(path).pixel_array[run["ink"]] == run["value"]).all()
    assert [r for r in run["rows"] if r[0] == "WARNING"
            and "not redacted (action: report)" in r[2]]


def test_withhold_writes_nothing_and_reports_it(tmp_path):
    run = _run(tmp_path, "withhold")
    assert run["files"] == []
    assert [r for r in run["rows"] if r[0] == "WARNING"
            and "(action: withhold)" in r[2]]


def test_two_runs_give_the_same_boxes_uid_and_bytes(tmp_path):
    """Determinism: the redacted UID depends on the boxes, so a box that
    moved between runs would move the UID and the exported bytes."""
    first = _run(tmp_path, "redact", "first", secret=True)
    second = _run(tmp_path, "redact", "second", secret=True)
    assert first["record"] == second["record"]
    assert first["uid"] == second["uid"]
    [a], [b] = first["files"], second["files"]
    assert pydicom.dcmread(a).PixelData == pydicom.dcmread(b).PixelData

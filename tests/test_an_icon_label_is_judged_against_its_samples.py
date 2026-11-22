"""An icon's label is judged against its own sample count (#1081).

An Icon Image Sequence item whose Photometric Interpretation contradicts
its Samples per Pixel -- `RGB` over one sample, `MONOCHROME2` over three
-- was written as held, with no row, under PASS. Measured on c3305076 and
again on 3e4ee91a (3.12.14 and 3.14.7t): case A, a source file whose
one-sample icon is labelled `RGB`, nothing set by hand; case B, the same
label set with `set_attr`; case C, `MONOCHROME2` set on a three-sample
icon. The top level corrects such a label
(`pixel_geometry.resolve_photometric_interpretation`).

By the warn-and-write rule the icon is written as held, with one
`WARNING` naming the item, the label and the sample count, asked by the
same predicate the top level uses. The run grades `REVIEW_REQUIRED`. A
label already warned about as inadmissible gets that row alone, and an
item with no label at all is not judged.

Names `isocenter.io_handlers`.
"""
import json
import os

import pydicom
import pytest

from isocenter import exporters
from isocenter.session import DicomSession
from support import plugin_exporters as plugins
from test_export_photometric_admissibility import (ICON_PATH, ICON_SAMPLES,
                                                   _grade, _icon_image,
                                                   _icon_source)

REMEDY = ("An icon is written uncompressed whatever transfer syntax the file "
          "carries")


def _contradiction(label, samples):
    names = "a one-sample image" if samples >= 3 else "a colour image"
    return (f"PhotometricInterpretation '{label}' on the icon at {ICON_PATH} "
            f"names {names}, and the item declares {samples} "
            f"{'sample' if samples == 1 else 'samples'} per pixel. The label "
            f"was written as declared, over the samples the item held, and "
            f"neither was changed. ")


def _label_rows(rows):
    return [r for r in rows if "on the icon at" in r[2]]


def _relabelled_source(folder, label, samples=1):
    """Case A: the source file itself labels its icon `label`."""
    _icon_source(folder, samples=samples)
    path = os.path.join(folder, "one.dcm")
    ds = pydicom.dcmread(path)
    ds.IconImageSequence[0].PhotometricInterpretation = label
    ds.save_as(path, enforce_file_format=True)


def test_case_a_a_source_files_icon_gets_the_warning(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _relabelled_source(str(src), "RGB")
    report = tmp_path / "r.md"
    with DicomSession(str(tmp_path / "a.db")) as session:
        session.ingest(str(src))
        session.export(str(tmp_path / "out"), use_compression=False,
                       show_progress=False)
        rows = [tuple(r) for r in session.store_backend.get_audit_errors()]
        session.generate_report(str(report))
    labelled = _label_rows(rows)
    assert len(labelled) == 1, rows
    assert labelled[0][1] == "WARNING"
    assert labelled[0][2].startswith(_contradiction("RGB", 1))
    assert REMEDY in labelled[0][2]
    assert "REVIEW_REQUIRED" in _grade(report)
    written = [os.path.join(r, f) for r, _d, fs in os.walk(tmp_path / "out")
               for f in fs if f.endswith(".dcm")]
    icon = pydicom.dcmread(written[0]).IconImageSequence[0]
    assert icon.PhotometricInterpretation == "RGB"
    assert icon.SamplesPerPixel == 1
    assert icon.PixelData == ICON_SAMPLES[1]


@pytest.mark.parametrize("label, samples", [("RGB", 1), ("MONOCHROME2", 3),
                                            ("monochrome1 ", 3)],
                         ids=["B", "C", "C-respelled"])
def test_cases_b_and_c_set_by_hand_get_the_warning(tmp_path, label, samples):
    """Red if `_icon_label_samples_warning` is not asked."""
    rows, exported, grade, _summary = _icon_image(tmp_path, label, samples)
    labelled = _label_rows(rows)
    assert len(labelled) == 1, rows
    assert labelled[0][1] == "WARNING"
    written = exported.IconImageSequence[0].PhotometricInterpretation
    assert labelled[0][2].startswith(_contradiction(written, samples))
    assert exported.IconImageSequence[0].SamplesPerPixel == samples
    assert exported.IconImageSequence[0].PixelData == ICON_SAMPLES[samples]
    assert "REVIEW_REQUIRED" in grade, grade


@pytest.mark.parametrize("label, samples", [
    ("RGB", 3), ("MONOCHROME2", 1), ("MONOCHROME1", 1), ("PALETTE COLOR", 1),
    ("YBR_FULL", 3)])
def test_an_agreeing_label_draws_no_row(tmp_path, label, samples):
    rows, _exported, grade, _summary = _icon_image(tmp_path, label, samples)
    assert _label_rows(rows) == [], rows
    assert "PASS" in grade, grade


def test_a_label_already_warned_as_inadmissible_gets_one_row(tmp_path):
    """`YBR_ICT` over one sample: the inadmissible label's row alone."""
    rows, _exported, _grade_line, _summary = _icon_image(
        tmp_path, "YBR_ICT", 1)
    labelled = _label_rows(rows)
    assert len(labelled) == 1, rows
    assert "is not a label uncompressed pixel data admits" in labelled[0][2]


def test_an_icon_with_no_label_draws_no_warning(tmp_path):
    """The predicate's default for an absent label is not a contradiction."""
    src = tmp_path / "src"
    src.mkdir()
    _icon_source(str(src), samples=1)
    path = os.path.join(str(src), "one.dcm")
    ds = pydicom.dcmread(path)
    del ds.IconImageSequence[0].PhotometricInterpretation
    ds.save_as(path, enforce_file_format=True)
    with DicomSession(str(tmp_path / "n.db")) as session:
        session.ingest(str(src))
        session.export(str(tmp_path / "out"), use_compression=False,
                       show_progress=False)
        rows = [tuple(r) for r in session.store_backend.get_audit_errors()]
    assert _label_rows(rows) == [], rows


@pytest.fixture
def _recorder():
    before = dict(exporters._REGISTRY)  # pylint: disable=protected-access
    exporters.register("recorder", plugins.Recorder)
    yield
    exporters._REGISTRY.clear()  # pylint: disable=protected-access
    exporters._REGISTRY.update(before)  # pylint: disable=protected-access


def test_a_plugin_export_gets_the_same_row(tmp_path, _recorder):  # pylint: disable=redefined-outer-name
    """The judgement is in the build half (#1197), so a plugin gets it."""
    src = tmp_path / "src"
    src.mkdir()
    _relabelled_source(str(src), "RGB")
    with DicomSession(str(tmp_path / "p.db")) as session:
        session.ingest(str(src))
        session.export(str(tmp_path / "out"), format="recorder",
                       show_progress=False)
        rows = [tuple(r) for r in session.store_backend.get_audit_errors()]
    labelled = _label_rows(rows)
    assert len(labelled) == 1, rows
    assert labelled[0][2].startswith(_contradiction("RGB", 1))
    records = [os.path.join(r, f) for r, _d, fs in os.walk(tmp_path / "out")
               for f in fs if f.endswith(".json")]
    with open(records[0], encoding="utf-8") as handle:
        assert json.load(handle)["icons"] == 1

"""`compression` is None or `"j2k"`, and anything else is refused (#605).

Measured at c8b4f15 and again at 220a20f, 3.12.14: `compression="rle"`
or `"J2K"` through the export worker returned `ok=True` with every
channel empty and wrote an image with **no Pixel Data**. The worker
asked "is this compressed" three ways -- `written_syntax` and
`_finalize_dataset` compared `== "j2k"`, the integer arm tested
truthiness -- so any other truthy value skipped both the raw write and
the encoder. `write_tree(compression=...)` and `export_batch` pass the
value straight through, and `write_tree` has no readback to notice.

The fix is one predicate, `_compresses`, which answers True for
`"j2k"`, False for None, and raises `ValueError` for everything else,
called at every door a value can come through: `ExportContext`
construction (so `export_batch` and a hand-driven worker),
`write_tree`'s entry (so an empty tree refuses too), the worker itself
(a context is a plain dataclass and can be edited after construction),
and `_finalize_dataset`.

It is also what makes bunch G1's two signals true. The #534 pixel-less
judgement and the #596 16-bit note both key on "no pixel arm set
`written_pixels`". With `"rle"` the integer arm set it and wrote no
element, so the note described pixels the file did not carry; with only
None and `"j2k"` reaching the arm, setting `written_pixels` and writing
a pixel element are the same event.
"""
import itertools
from datetime import date

import numpy as np
import pydicom
import pytest

from isocenter.entities import Instance, Patient, Series, Study
from isocenter.io_handlers import (DicomExporter, ExportContext,
                                   _compresses, _export_instance_worker)

SC_STORAGE = "1.2.840.10008.5.1.4.1.1.7"
SR_STORAGE = "1.2.840.10008.5.1.4.1.1.88.11"
J2K_LOSSLESS = "1.2.840.10008.1.2.4.90"
IMPLICIT_VR_LE = "1.2.840.10008.1.2"

#: Every spelling a caller could reasonably try, and the two falsy
#: values that used to mean "native" by accident of the truthiness test.
REFUSED = ("rle", "J2K", "jpeg2000", " j2k", "", False, 0, True)

_serial = itertools.count(1)


def _image(label="RGB", arr=None):
    if arr is None:
        arr = np.full((8, 8, 3), (10, 20, 30), np.uint8)
    inst = Instance(f"1.2.826.0.1.605.{next(_serial)}", SC_STORAGE, 1)
    inst.file_path = None
    samples = arr.shape[-1] if arr.ndim == 3 else 1
    for tag, value in (("0008,0020", "20230101"), ("0008,0030", "120000"),
                       ("0008,0060", "OT"), ("0028,0002", samples)):
        inst.set_attr(tag, value)
    inst.set_pixel_data(arr)
    inst.set_attr("0028,0004", label)
    return inst


def _context(tmp_path, inst, **kwargs):
    return ExportContext(
        instance=inst,
        output_path=str(tmp_path / "out" / f"{inst.sop_instance_uid}.dcm"),
        patient_attributes={"0010,0010": "ANON", "0010,0020": "PAT1"},
        study_attributes={"0020,000d": "1.2.826.0.2.1"},
        series_attributes={"0020,000e": "1.2.826.0.3.1"},
        **kwargs)


def _patient(instances):
    patient = Patient("PAT1", "ANON")
    study = Study("1.2.826.0.2.605", date(2023, 1, 1))
    study.study_time = "120000"
    series = Series("1.2.826.0.3.605", "OT", 1)
    series.instances.extend(instances)
    study.series.append(series)
    patient.studies.append(study)
    return patient


def _names_the_accepted_values(message):
    return "None" in message and "'j2k'" in message


def test_the_predicate_has_two_answers_and_refuses_the_rest():
    """The one spelling of "is this compressed". Killing mutations: a
    truthiness test (`"rle"` answers True), a case-insensitive compare
    (`"J2K"` answers True), and a falsy value read as None."""
    assert _compresses(None) is False
    assert _compresses("j2k") is True
    for value in REFUSED:
        with pytest.raises(ValueError) as caught:
            _compresses(value)
        assert _names_the_accepted_values(str(caught.value)), caught.value


@pytest.mark.parametrize("value", REFUSED)
def test_a_context_refuses_an_unknown_compression(tmp_path, value):
    """`ExportContext` is what `export_batch` takes, so refusing at
    construction is refusing at that door. Killing mutation: no
    `__post_init__` check."""
    with pytest.raises(ValueError) as caught:
        _context(tmp_path, _image(), compression=value)
    assert _names_the_accepted_values(str(caught.value)), caught.value


def test_write_tree_refuses_before_writing_anything(tmp_path):
    """The door with no readback, where the loss was silent. It refuses
    on entry -- over a tree with instances and over one with none, so
    the refusal does not depend on a context being built. Killing
    mutation: no check in `write_tree` (an empty tree returns quietly)."""
    out = tmp_path / "tree"
    with pytest.raises(ValueError) as caught:
        DicomExporter.write_tree(_patient([_image()]), str(out),
                                 compression="rle", show_progress=False)
    assert _names_the_accepted_values(str(caught.value)), caught.value
    assert not out.exists() or not any(out.rglob("*.dcm"))

    with pytest.raises(ValueError):
        DicomExporter.write_tree(Patient("PAT2", "ANON"),
                                 str(tmp_path / "empty"),
                                 compression="J2K", show_progress=False)


def test_the_worker_refuses_a_context_edited_after_construction(tmp_path):
    """A dataclass can be edited after `__post_init__`, so the worker asks
    too, and inside its `try`: the instance fails with its own error
    rather than taking the batch down or writing a file with no pixels.

    What this pins is the *outcome* -- refused, by the instance, with the
    accepted values named -- not the worker's own call, which is an
    equivalent mutant for the refusal: restore the worker's `== "j2k"`
    and `_finalize_dataset` raises the same `ValueError` inside the same
    `try`, so this test stays green (measured: M605f survives). The
    worker's call earns its place by being the one predicate
    `written_syntax` and the integer arm both key on, which is what
    `test_every_accepted_value_writes_the_pixels_the_arm_judged` holds."""
    inst = _image()
    ctx = _context(tmp_path, inst)
    ctx.compression = "rle"

    outcome = _export_instance_worker(ctx)

    assert not outcome.ok
    assert isinstance(outcome.error, ValueError), outcome.error
    assert _names_the_accepted_values(str(outcome.error)), outcome.error
    assert not (tmp_path / "out").exists() or not any(
        (tmp_path / "out").rglob("*.dcm"))


def test_finalize_refuses_an_unknown_compression():
    """`_finalize_dataset` is the encoder's gate and asks the same
    predicate. Killing mutation: its `== 'j2k'` compare restored, which
    writes native for `"rle"` without a word."""
    with pytest.raises(ValueError):
        DicomExporter._finalize_dataset(pydicom.Dataset(), "rle")


@pytest.mark.parametrize("compression", [None, "j2k"])
def test_every_accepted_value_writes_the_pixels_the_arm_judged(
        tmp_path, compression):
    """Bunch G1's signals, checked on the two values that can now reach
    the arm. A 16-bit `YBR_FULL` image: the file carries Pixel Data, and
    the #596 note (keyed on `written_pixels`) fires once, so the note
    describes pixels that are there. A pixel-less instance declaring
    `YBR_ICT`: no pixel element, and the #534 judgement (keyed on
    `written_pixels is None`) warns once, against the syntax the file
    carries -- native even under `"j2k"`."""
    image = _image("YBR_FULL", arr=np.full((8, 8, 3), 300, np.uint16))
    outcome = _export_instance_worker(
        _context(tmp_path, image, compression=compression))
    assert outcome.ok, outcome.error
    written = pydicom.dcmread(outcome.output_path)
    assert "PixelData" in written
    assert len([c for c in outcome.corrections if "#461" in c]) == 1, \
        outcome.corrections

    report = Instance(f"1.2.826.0.1.605.{next(_serial)}", SR_STORAGE, 1)
    report.file_path = None
    for tag, value in (("0008,0020", "20230101"), ("0008,0030", "120000"),
                       ("0008,0060", "SR"), ("0028,0004", "YBR_ICT")):
        report.set_attr(tag, value)
    outcome = _export_instance_worker(
        _context(tmp_path, report, compression=compression))
    assert outcome.ok, outcome.error
    written = pydicom.dcmread(outcome.output_path)
    assert "PixelData" not in written
    assert written.file_meta.TransferSyntaxUID == IMPLICIT_VR_LE
    assert len(outcome.warnings) == 1, outcome.warnings
    assert not [c for c in outcome.corrections if "#461" in c]

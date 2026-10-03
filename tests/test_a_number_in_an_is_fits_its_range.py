"""A Python number set into a standard IS element is written as an integer in IS's range (#897).

An Integer String is at most 12 characters and names an integer from
-2^31 to 2^31-1 (PS3.5 Table 6.2-1). pydicom writes whatever number it is
handed: measured on `main` at 7e66fec6, live and reopened, under both
transfer syntaxes and with no row, `1e20` set into Exposure Time went out
as `'100000000000000000000'` (21 characters), `1.5` as `'1.5'` (a decimal
in an integer VR), `123456789012.0` as `'123456789012'` (12 characters,
past 2^31-1) and the int `2**40` as `'1099511627776'`.

The owner's ruling (Q1, A): a non-integer is rounded half-to-even and
written, with a `WARNING` row naming the value set and the value written;
a value outside the range, or non-finite, is dropped with a `DATA_LOSS`
row. Numbers only: a `str` is a configuration's shape and is left alone
(a follow-up), and a value read from a file keeps its source text
(`original_string`, #662), as #723 does for DS.

Every assertion reads the element's bytes as written (`get_item`), never a
parsed number.
"""
import logging

import numpy as np
import pydicom
import pytest
from pydicom.data import get_testdata_file
from pydicom.dataelem import RawDataElement
from pydicom.tag import Tag

from isocenter.entities import DicomItem
from isocenter.session import DicomSession

#: What every #897 rounding row says.
_ROUND_WORDS = "cannot be written in an Integer String, and was rounded"


def _source(src, edit=None):
    src.mkdir(parents=True, exist_ok=True)
    ds = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    if edit is not None:
        edit(ds)
    ds.save_as(str(src / "a.dcm"), enforce_file_format=True)


def _instance(session):
    (patient,) = session.store.patients
    return patient.studies[0].series[0].instances[0]


def _export(tmp_path, mutate=None, *, reopen=False, caplog=None):
    """Ingest CT_small, apply `mutate(instance)`, export uncompressed.

    Returns the written dataset (raw), the rounding WARNING rows, the
    DATA_LOSS rows and every audit row.
    """
    if not (tmp_path / "src").exists():
        _source(tmp_path / "src")
    db = str(tmp_path / "s.db")
    out = tmp_path / ("out-r" if reopen else "out")
    with caplog.at_level(logging.INFO, logger="isocenter"):
        with DicomSession(db) as s:
            if not s.store.patients:
                s.ingest(str(tmp_path / "src"))
                if mutate is not None:
                    mutate(_instance(s))
            if reopen:
                s.save(sync=True)
            else:
                s.export(str(out), use_compression=False, show_progress=False)
                rows = s.store_backend.get_audit_errors()
                losses = s.store_backend.get_audit_losses()
        if reopen:
            with DicomSession(db) as s:
                s.export(str(out), use_compression=False, show_progress=False)
                rows = s.store_backend.get_audit_errors()
                losses = s.store_backend.get_audit_losses()
    warnings = [r for r in rows if r[1] == "WARNING" and _ROUND_WORDS in r[2]]
    (written,) = list(out.rglob("*.dcm"))
    return pydicom.dcmread(str(written)), warnings, losses, rows


def _raw(ds, tag):
    return ds.get_item(tag).value


def _dropped(losses, tag):
    return [r for r in losses if f"Tag {tag} not exported" in r[2]]


@pytest.mark.parametrize("reopen", [False, True], ids=["live", "reopened"])
def test_a_number_past_is_range_is_dropped_with_a_row(tmp_path, caplog, reopen):
    """The issue's value: `1e20` was written as 21 characters."""
    ds, warnings, losses, _ = _export(
        tmp_path, lambda i: i.set_attr("0018,1150", 1e20),
        reopen=reopen, caplog=caplog)
    assert 0x00181150 not in ds
    (row,) = _dropped(losses, "0018,1150")
    assert "1e+20 is outside IS's range, -2147483648 to 2147483647" in row[2]
    assert row[3] == "STANDARD"
    assert warnings == []


@pytest.mark.parametrize("reopen", [False, True], ids=["live", "reopened"])
def test_a_non_integer_is_rounded_with_a_warning(tmp_path, caplog, reopen):
    ds, warnings, losses, _ = _export(
        tmp_path, lambda i: i.set_attr("0018,1150", 1.5),
        reopen=reopen, caplog=caplog)
    assert _raw(ds, 0x00181150) == b"2 "
    assert len(warnings) == 1, warnings
    assert warnings[0][2] == (
        "Tag 0018,1150 (IS): a number that is not an integer cannot be "
        "written in an Integer String, and was rounded: 1.5 as '2'."), warnings
    assert _dropped(losses, "0018,1150") == []


@pytest.mark.parametrize("value, written", [
    (2.5, b"2 "),
    (3.5, b"4 "),
    (-1.5, b"-2"),
], ids=["2.5", "3.5", "-1.5"])
def test_rounding_is_half_to_even(tmp_path, caplog, value, written):
    """Half-to-even, pinned so the rule is stated."""
    ds, warnings, _losses, _ = _export(
        tmp_path, lambda i: i.set_attr("0018,1150", value), caplog=caplog)
    assert _raw(ds, 0x00181150) == written
    assert len(warnings) == 1, warnings


@pytest.mark.parametrize("value, text", [
    (np.float32(1.5), "2"),
    (np.float64(2.5), "2"),
    (np.int32(7), "7"),
], ids=["float32-1.5", "float64-2.5", "int32-7"])
def test_a_numpy_number_is_written_as_an_integer(value, text):
    """A numpy number is written as an integer. Under numpy 1.x, which
    `setup.py` admits, `round()` of a numpy float is a numpy float whose
    text is `'2.0'`; that case is the reason for `int(round(...))`, and is
    seen only on a numpy 1.x install. Driven through `_merge` directly,
    because the store does not save a numpy scalar (`json` refuses it), so
    no session export can carry one."""
    from isocenter.io_handlers import DicomExporter

    ds, losses, warnings = pydicom.Dataset(), [], []
    DicomExporter._merge(ds, {"0018,1150": value}, losses, warnings=warnings)
    assert losses == []
    assert str(ds[0x00181150].value) == text
    assert len(warnings) == (0 if text == "7" else 1), warnings


def test_an_int_past_a_float_is_dropped_with_the_range_words():
    """`10**400` cannot become a float; it is outside IS's range like any
    other, and the row says so rather than an OverflowError (#924 review)."""
    from isocenter.io_handlers import DicomExporter

    ds, losses = pydicom.Dataset(), []
    DicomExporter._merge(ds, {"0018,1150": 10 ** 400}, losses)
    assert 0x00181150 not in ds
    (loss,) = losses
    assert "is outside IS's range, -2147483648 to 2147483647" in loss[1], losses
    assert "OverflowError" not in loss[1]


def test_a_numpy_integer_past_the_range_is_dropped():
    from isocenter.io_handlers import DicomExporter

    ds, losses = pydicom.Dataset(), []
    DicomExporter._merge(ds, {"0018,1150": np.int64(2 ** 40)}, losses)
    assert 0x00181150 not in ds
    (loss,) = losses
    assert "np.int64(1099511627776) is outside IS's range" in loss[1], losses


_LO, _HI = -2 ** 31, 2 ** 31 - 1


@pytest.mark.parametrize("value, written", [
    (_HI, b"2147483647"),
    (float(_HI), b"2147483647"),
    (_LO, b"-2147483648 "),
    (float(_LO), b"-2147483648 "),
    (2147483647.4, b"2147483647"),
    (_HI + 1, None),
    (float(_HI + 1), None),
    (_LO - 1, None),
    (float(_LO - 1), None),
    (2147483647.6, None),
    (123456789012.0, None),
], ids=["hi", "hi-float", "lo", "lo-float", "hi+0.4-rounds-in",
        "hi+1", "hi+1-float", "lo-1", "lo-1-float", "hi+0.6-rounds-out",
        "12-chars-past-range"])
def test_the_range_is_inclusive_and_is_judged_after_rounding(
        tmp_path, caplog, value, written):
    """Round, then check the range: `2147483647.4` rounds into it and is
    written; `2147483647.6` rounds out and is dropped. `123456789012.0`
    fits 12 characters and not the range, which a length test alone
    would pass."""
    ds, _warnings, losses, _ = _export(
        tmp_path, lambda i: i.set_attr("0018,1150", value), caplog=caplog)
    if written is None:
        assert 0x00181150 not in ds
        (row,) = _dropped(losses, "0018,1150")
        assert "outside IS's range" in row[2]
    else:
        assert _raw(ds, 0x00181150) == written
        assert _dropped(losses, "0018,1150") == []


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")],
                         ids=["nan", "inf", "-inf"])
def test_a_non_finite_number_is_dropped_with_a_row_naming_it(tmp_path, caplog, value):
    ds, _warnings, losses, _ = _export(
        tmp_path, lambda i: i.set_attr("0018,1150", value), caplog=caplog)
    assert 0x00181150 not in ds
    (row,) = _dropped(losses, "0018,1150")
    assert f"{value!r} has no Integer String spelling" in row[2]


def test_each_value_of_a_multi_valued_is_is_fitted(tmp_path, caplog):
    ds, warnings, _losses, _ = _export(
        tmp_path, lambda i: i.set_attr("0028,0034", [1.5, 1]), caplog=caplog)
    assert _raw(ds, 0x00280034) == b"2\\1 "
    assert len(warnings) == 1, warnings
    assert warnings[0][2].endswith("rounded: 1.5 as '2'."), warnings


def test_an_is_in_a_sequence_item_is_fitted(tmp_path, caplog):
    def mutate(inst):
        item = DicomItem()
        item.set_attr("0008,1150", "1.2.840.10008.5.1.4.1.1.2")
        item.set_attr("0008,1155", "1.2.826.0.1.897.9")
        item.set_attr("0008,1160", 1.5)
        inst.add_sequence_item("0008,1140", item)

    ds, warnings, _losses, _ = _export(tmp_path, mutate, caplog=caplog)
    item = ds.ReferencedImageSequence[0]
    assert item.get_item(0x00081160).value in (b"2 ", "2")
    assert len(warnings) == 1, warnings
    assert "Tag (0008,1140) > 0008,1160 (IS)" in warnings[0][2]


@pytest.mark.parametrize("reopen", [False, True], ids=["live", "reopened"])
def test_a_source_is_out_of_range_keeps_its_source_text(tmp_path, caplog, reopen):
    """The one fixture that kills deleting the `original_string` exemption:
    the file's own IS, 11 characters and past 2^31-1, is its statement and
    not ours to drop."""
    text = b"99999999999 "

    def edit(ds):
        ds._dict[Tag(0x00181150)] = RawDataElement(
            Tag(0x00181150), "IS", len(text), text, 0, False, True)

    _source(tmp_path / "src", edit)
    ds, warnings, losses, _ = _export(tmp_path, reopen=reopen, caplog=caplog)
    assert _raw(ds, 0x00181150) == text
    assert warnings == [] and _dropped(losses, "0018,1150") == []


@pytest.mark.parametrize("reopen", [False, True], ids=["live", "reopened"])
@pytest.mark.parametrize("value", [7.0, 7], ids=["float", "int"])
def test_a_whole_number_in_range_is_written_as_before(tmp_path, caplog, value, reopen):
    ds, warnings, losses, rows = _export(
        tmp_path, lambda i: i.set_attr("0018,1150", value),
        reopen=reopen, caplog=caplog)
    assert _raw(ds, 0x00181150) == b"7 "
    assert warnings == [] and _dropped(losses, "0018,1150") == []
    assert not [r for r in rows if "0018,1150" in r[2]], rows
    assert not [r for r in caplog.records if "0018,1150 (IS)" in r.getMessage()]


def test_a_bool_is_not_a_number_here(tmp_path, caplog):
    """`bool` is excluded, so `True` is written as `main` wrote it
    (measured: `'1'`), with no row."""
    ds, warnings, losses, _ = _export(
        tmp_path, lambda i: i.set_attr("0018,1150", True), caplog=caplog)
    assert _raw(ds, 0x00181150) == b"1 "
    assert warnings == [] and _dropped(losses, "0018,1150") == []


def test_a_caller_text_is_left_alone(tmp_path, caplog):
    """Numbers only: a `str` is what a configuration writes, and checking
    its range would change what a loaded file exports (`CONFIG_VERSION`,
    #762). Written as `main` wrote it."""
    ds, warnings, losses, _ = _export(
        tmp_path, lambda i: i.set_attr("0018,1150", "2147483648"), caplog=caplog)
    assert _raw(ds, 0x00181150) == b"2147483648"
    assert warnings == [] and _dropped(losses, "0018,1150") == []

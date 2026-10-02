"""A Python float set into a standard DS element is written in 16 characters or fewer (#723).

DS holds at most 16 characters (PS3.5 Table 6.2-1). pydicom writes a Python
`float` with `repr`, so a value a caller sets -- `set_attr("0018,0060",
0.1 + 0.2)` -- went out as `'0.30000000000000004'`, 19 characters, with no
row and no note, live and reopened, under both transfer syntaxes. Measured on
`main` at 7579d4df.

The owner's ruling (Q3): a whole number that fits is written in its integer
spelling, which is exact, with an INFO note; anything else is rounded with
pydicom's `format_number_as_ds`, with a `WARNING` row naming the value set
and the value written. A non-finite float has no DS spelling and is dropped
with a `DATA_LOSS` row. A value read from a file keeps its source text
(`original_string`, #662), even an over-long one, and a float that already
fits is written as before.

Every assertion reads the element's bytes as written (`get_item`), never a
parsed number.
"""
import logging

import pydicom
import pytest
from pydicom.data import get_testdata_file
from pydicom.dataelem import RawDataElement
from pydicom.tag import Tag

from isocenter.entities import DicomItem
from isocenter.session import DicomSession


#: What every #723/#898 note and row says, whether the value set was a
#: float or text.
_FIT_WORDS = "longer than DS's 16 characters"


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

    Returns the written dataset (raw), the INFO notes and the WARNING and
    DATA_LOSS rows.
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
    notes = [r.getMessage() for r in caplog.records
             if _FIT_WORDS in r.getMessage() and r.levelno == logging.INFO]
    warnings = [r for r in rows if r[1] == "WARNING" and _FIT_WORDS in r[2]]
    (written,) = list(out.rglob("*.dcm"))
    return pydicom.dcmread(str(written)), notes, warnings, losses


def _raw(ds, tag):
    return ds.get_item(tag).value


@pytest.mark.parametrize("reopen", [False, True], ids=["live", "reopened"])
def test_a_whole_number_is_written_in_its_integer_spelling(tmp_path, caplog, reopen):
    ds, notes, warnings, _ = _export(
        tmp_path, lambda i: i.set_attr("0018,0050", 1234567890123456.0),
        reopen=reopen, caplog=caplog)
    assert _raw(ds, 0x00180050) == b"1234567890123456"
    assert len(notes) == 1 and "0018,0050" in notes[0], notes
    assert warnings == []


@pytest.mark.parametrize("reopen", [False, True], ids=["live", "reopened"])
def test_a_long_fraction_is_rounded_with_a_warning(tmp_path, caplog, reopen):
    ds, notes, warnings, _ = _export(
        tmp_path, lambda i: i.set_attr("0018,0060", 0.1 + 0.2),
        reopen=reopen, caplog=caplog)
    assert _raw(ds, 0x00180060) == b"0.30000000000000"
    assert len(warnings) == 1, warnings
    detail = warnings[0][2]
    assert "0018,0060" in detail
    assert "0.30000000000000004" in detail
    assert "0.30000000000000" in detail
    assert notes == []


def test_each_value_of_a_multi_valued_ds_is_written_to_fit(tmp_path, caplog):
    ds, _notes, warnings, _ = _export(
        tmp_path, lambda i: i.set_attr("0028,0030", [0.1 + 0.2, 1 / 3]),
        caplog=caplog)
    # 33 characters, padded to even length.
    assert _raw(ds, 0x00280030) == b"0.30000000000000\\0.33333333333333 "
    assert len(warnings) == 1, warnings


def test_a_ds_in_a_sequence_item_is_written_to_fit(tmp_path, caplog):
    def mutate(inst):
        item = DicomItem()
        item.set_attr("0008,1150", "1.2.840.10008.5.1.4.1.1.2")
        item.set_attr("0008,1155", "1.2.826.0.1.723.9")
        item.set_attr("0018,0050", 1234567890123456.0)
        inst.add_sequence_item("0008,1140", item)

    ds, notes, _warnings, _ = _export(tmp_path, mutate, caplog=caplog)
    item = ds.ReferencedImageSequence[0]
    assert item.get_item(0x00180050).value in (b"1234567890123456",
                                               "1234567890123456")
    assert str(item.SliceThickness) == "1234567890123456"
    assert len(notes) == 1, notes


@pytest.mark.parametrize("reopen", [False, True], ids=["live", "reopened"])
def test_an_over_long_source_ds_keeps_its_source_text(tmp_path, caplog, reopen):
    """The one fixture that kills deleting the `original_string` exemption:
    `str()` of this value is 19 characters, so the length test alone would
    rewrite a value the source file stated."""
    text = b"0.30000000000000004 "

    def edit(ds):
        ds._dict[Tag(0x00180050)] = RawDataElement(
            Tag(0x00180050), "DS", len(text), text, 0, False, True)

    _source(tmp_path / "src", edit)
    ds, notes, warnings, _ = _export(tmp_path, reopen=reopen, caplog=caplog)
    assert _raw(ds, 0x00180050) == text
    assert notes == [] and warnings == []


@pytest.mark.parametrize("value", [float("nan"), float("inf")], ids=["nan", "inf"])
def test_a_non_finite_float_is_dropped_with_a_row(tmp_path, caplog, value):
    # Spacing Between Slices, Type 3 on a CT: Slice Thickness is Type 2,
    # and the export would refill it zero-length.
    ds, _notes, _warnings, losses = _export(
        tmp_path, lambda i: i.set_attr("0018,0088", value), caplog=caplog)
    assert 0x00180088 not in ds
    rows = [r for r in losses if "Tag 0018,0088 not exported" in r[2]]
    assert len(rows) == 1, losses


def test_a_float_that_fits_is_written_as_before(tmp_path, caplog):
    ds, notes, warnings, _ = _export(
        tmp_path, lambda i: i.set_attr("0028,1050", 1e-07), caplog=caplog)
    assert _raw(ds, 0x00281050) == b"1e-07 "
    assert notes == [] and warnings == []


@pytest.mark.parametrize("reopen", [False, True], ids=["live", "reopened"])
def test_a_source_private_ds_inf_is_written_as_read(tmp_path, caplog, reopen):
    """A private `DS` the file wrote as `inf` keeps its VR and its text.

    It carries `original_string`, the exemption `_ds_text_that_fits` honours;
    the recorded-VR gate honours it too, so the live export and the
    reopened one agree, and both write what `main` wrote (review of #900,
    F1: live went to `LO` while reopened stayed `DS`).
    """
    # Explicit VR source and a compressed (Explicit VR) export: the private
    # VR is recorded only from an explicit source (#676), and only an
    # explicit file says what VR was written.
    src = tmp_path / "src"
    src.mkdir()
    source = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    source.add_new(0x00090010, "LO", "ACME")
    source.add_new(0x00091001, "DS", "inf")
    source.save_as(str(src / "a.dcm"), enforce_file_format=True,
                   implicit_vr=False, little_endian=True)
    db, out = str(tmp_path / "s.db"), tmp_path / "out"
    with DicomSession(db) as s:
        s.ingest(str(src))
        if not reopen:
            s.export(str(out), show_progress=False)
            losses = s.store_backend.get_audit_losses()
        else:
            s.save(sync=True)
    if reopen:
        with DicomSession(db) as s:
            s.export(str(out), show_progress=False)
            losses = s.store_backend.get_audit_losses()
    (written,) = list(out.rglob("*.dcm"))
    elem = pydicom.dcmread(str(written)).get_item(0x00091001)
    assert elem.VR == "DS"
    assert elem.value.strip() == b"inf"
    assert not [r for r in losses if "0009,1001" in r[2]], losses


@pytest.mark.parametrize("value", [float("inf"), float("-inf")], ids=["inf", "-inf"])
def test_a_non_finite_float_under_a_recorded_private_ds_takes_the_fallback(value):
    """The private-VR gate declines what `_merge` would drop under `DS`.

    `_value_fits_vr` said yes to `inf` under a recorded private `DS` (three
    characters), and the #723 rewrite then dropped it with a `DATA_LOSS`
    row: the gate and the writer disagreeing, the shape
    `test_the_gate_and_the_writer_agree_across_the_whole_vr_table` pins.
    Declined, it takes `_fallback_encoding`, the path of a private tag with
    no recorded VR, which writes its text under `LO` -- the same three or
    four characters `main` wrote under `DS`, now under a VR that may hold
    them.
    """
    from isocenter.io_handlers import DicomExporter, _value_fits_vr

    assert not _value_fits_vr(value, "DS")
    ds, losses = pydicom.Dataset(), []
    DicomExporter._merge(ds, {"0009,1001": value}, losses,
                         vrs={"0009,1001": "DS"})
    assert losses == []
    assert ds[0x00091001].VR == "LO"
    assert ds[0x00091001].value == str(value)


# --- #898: a caller's `str` over 16 characters takes #723's rule ----------
#
# pydicom turns a `str` set into a DS into a `DSfloat` whose
# `original_string` is the caller's text, and writes that text whatever its
# length: `set_attr(t, '0.30000000000000004')` went out as 19 characters
# where `set_attr(t, 0.1 + 0.2)` went out as 16 (owner ruling Q2, A). The
# length is judged on the stripped text, because pydicom strips the padding.


@pytest.mark.parametrize("reopen", [False, True], ids=["live", "reopened"])
def test_a_long_decimal_text_is_rounded_as_its_float_is(tmp_path, caplog, reopen):
    ds, notes, warnings, _ = _export(
        tmp_path, lambda i: i.set_attr("0018,0050", "0.30000000000000004"),
        reopen=reopen, caplog=caplog)
    text = _raw(ds, 0x00180050)
    # Pinned to the literal first, so the comparison below cannot hold at
    # two equally wrong spellings.
    assert text == b"0.30000000000000"
    assert len(warnings) == 1, warnings
    assert ("Tag 0018,0050 (DS): a value longer than DS's 16 characters "
            "cannot be written exactly, and was rounded to fit: "
            "'0.30000000000000004' as '0.30000000000000'.") in warnings[0][2]
    assert notes == []
    as_float, *_ = _export(
        tmp_path / "float", lambda i: i.set_attr("0018,0050", 0.1 + 0.2),
        reopen=reopen, caplog=caplog)
    assert _raw(as_float, 0x00180050) == text


@pytest.mark.parametrize("reopen", [False, True], ids=["live", "reopened"])
def test_a_long_whole_number_text_is_written_in_its_integer_spelling(
        tmp_path, caplog, reopen):
    ds, notes, warnings, _ = _export(
        tmp_path, lambda i: i.set_attr("0018,0050", "1234567890123456.0"),
        reopen=reopen, caplog=caplog)
    assert _raw(ds, 0x00180050) == b"1234567890123456"
    # The parent prefixes the SOP Instance UID.
    assert len(notes) == 1, notes
    assert notes[0].endswith(
        ": Tag 0018,0050 (DS): a value longer than DS's 16 characters was "
        "written in its integer spelling, the same number: "
        "'1234567890123456.0' as '1234567890123456'."), notes
    assert warnings == []


@pytest.mark.parametrize("reopen", [False, True], ids=["live", "reopened"])
def test_a_long_integer_text_past_a_float_is_rounded_with_a_warning(
        tmp_path, caplog, reopen):
    """17 digits: `float()` of it is 12345678901234568, and its integer
    spelling is 17 characters too, so `format_number_as_ds` writes it. The
    text set and the text written are not the same number, which is what
    the row says."""
    ds, notes, warnings, _ = _export(
        tmp_path, lambda i: i.set_attr("0018,0050", "12345678901234567"),
        reopen=reopen, caplog=caplog)
    assert _raw(ds, 0x00180050) == b"1.2345678901e+16"
    assert len(warnings) == 1, warnings
    assert "'12345678901234567' as '1.2345678901e+16'" in warnings[0][2]
    assert notes == []


def test_text_more_precise_than_a_float_is_rounded_with_a_warning(tmp_path, caplog):
    """19 characters naming a number a double cannot hold: it parses to the
    float 0.1, written `'0.1'`. The floats are equal, the numbers are not,
    so it is a WARNING row and not an INFO note -- exactness is judged on
    the decimal text."""
    ds, notes, warnings, _ = _export(
        tmp_path, lambda i: i.set_attr("0018,0050", "0.10000000000000001"),
        caplog=caplog)
    assert _raw(ds, 0x00180050) == b"0.1 "
    assert len(warnings) == 1, warnings
    assert "'0.10000000000000001' as '0.1'" in warnings[0][2]
    assert notes == []


@pytest.mark.parametrize("reopen", [False, True], ids=["live", "reopened"])
def test_padding_does_not_count_against_the_sixteen(tmp_path, caplog, reopen):
    """18 characters as set, 16 once stripped: pydicom writes the stripped
    text, which fits, so nothing is rewritten and nothing is said."""
    ds, notes, warnings, _ = _export(
        tmp_path, lambda i: i.set_attr("0018,0050", " 1234567890.12345 "),
        reopen=reopen, caplog=caplog)
    assert _raw(ds, 0x00180050) == b"1234567890.12345"
    assert notes == [] and warnings == []


@pytest.mark.parametrize("reopen", [False, True], ids=["live", "reopened"])
def test_each_value_of_a_backslash_text_is_fitted(tmp_path, caplog, reopen):
    ds, _notes, warnings, losses = _export(
        tmp_path, lambda i: i.set_attr("0028,0030", "0.30000000000000004\\1"),
        reopen=reopen, caplog=caplog)
    # 18 characters: even, so no padding.
    assert _raw(ds, 0x00280030) == b"0.30000000000000\\1"
    assert len(warnings) == 1, warnings
    assert "'0.30000000000000004' as '0.30000000000000'" in warnings[0][2]
    assert not [r for r in losses if "0028,0030" in r[2]], losses


def test_each_text_of_a_list_is_fitted(tmp_path, caplog):
    ds, _notes, warnings, _ = _export(
        tmp_path, lambda i: i.set_attr("0018,1100", ["0.30000000000000004"]),
        caplog=caplog)
    assert _raw(ds, 0x00181100) == b"0.30000000000000"
    assert len(warnings) == 1, warnings


def test_a_long_text_naming_no_finite_number_is_dropped_with_a_row(tmp_path, caplog):
    """`'1e400000000000000'` is 17 characters and `float()` of it is `inf`,
    which has no DS spelling: dropped with this element's DATA_LOSS row, as
    a non-finite float is. `main` wrote the 17 characters."""
    ds, _notes, _warnings, losses = _export(
        tmp_path, lambda i: i.set_attr("0018,0088", "1e400000000000000"),
        caplog=caplog)
    assert 0x00180088 not in ds
    rows = [r for r in losses if "Tag 0018,0088 not exported" in r[2]]
    assert len(rows) == 1, losses


@pytest.mark.parametrize("reopen", [False, True], ids=["live", "reopened"])
def test_an_over_long_multi_valued_source_ds_keeps_its_source_text(
        tmp_path, caplog, reopen):
    """A source's own two-valued DS over 16 characters is the file's
    statement. If a reopen handed it back as one backslash-joined `str`,
    the #898 text arm would rewrite it; this pins that it does not."""
    text = b"0.30000000000000004\\1 "

    def edit(ds):
        ds._dict[Tag(0x00280030)] = RawDataElement(
            Tag(0x00280030), "DS", len(text), text, 0, False, True)

    _source(tmp_path / "src", edit)
    ds, notes, warnings, _ = _export(tmp_path, reopen=reopen, caplog=caplog)
    assert _raw(ds, 0x00280030) == text
    assert notes == [] and warnings == []

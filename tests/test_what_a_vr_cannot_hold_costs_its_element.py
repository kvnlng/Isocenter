"""A value its VR cannot hold costs that element, never the file (X5).

The owner's tie-breaker (ruling of 2026-11-20 on #1018): when the exporter
reads a value it cannot write faithfully, it writes the closest honest
output and records the loss in one row. Measured at `main` 3e4ee91a on
3.12.14 and 3.14.7t (the X5 brief, section 0):

- a number, `str`, list, `Decimal` or `bool` under OB, OW, OF, OD, OL or
  OV failed the whole file in `dcmwrite` (#1018);
- under US, SS, UL, SL, UV and SV a float, text, bytes, an out-of-range
  integer or a `None` member failed the whole file, and so did text,
  bytes, `sNaN`, a complex number and an FL overflow under FL and FD
  (#986);
- a caller's `int` or `float` subclass under a text VR failed the whole
  file (#986);
- a numpy value that is not a number was written as a wrong value with no
  row (`timedelta64` as its count, `np.ma.masked` as `nan`, `complex64` as
  0, any array under a binary VR as its buffer), or failed the file
  (#1000; owner ruling Q1: an array under a binary VR loses its element
  too, and the caller passes `.tobytes()`);
- a file's own `IS` or `DSfloat` under LO, SH, CS, AS, AE, LT, ST, UT, UC
  or UR failed the whole file, and under PN and UI was lost in pydicom's
  words (#1017; owner ruling Q3: all fifteen text VRs write the file's
  own text);
- a `bytearray` failed the whole file under 13 text VRs and was written
  `65\\66` under IS and DS;
- under AT a float, a `bool` or a `None` was written as a group-0000 tag
  or failed the file (#1026), and a 2-tuple saved and reopened was two
  group-0000 tags (#1016, owner ruling Q5a);
- a list member holding a backslash was written as more values than the
  graph holds (#1014; owner ruling Q6: standard tags too);
- `_resolve_one_ambiguous_vr` took every `TypeError` for "the decider
  cannot decide" (#972, ruled 2026-10-07: narrow it; the `US or SS` arm,
  which converts the value, now re-raises);
- `_python_value` scanned a plain list it had nothing to do for (#1004).

Every row names the type and the VR, never the value. The helpers come
from `test_the_exporter_writes_the_python_value.py`, which pins the
cells this file does not reverse.
"""
import enum
import io
import struct
from decimal import Decimal

import numpy as np
import pydicom
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.multival import MultiValue
from pydicom.tag import BaseTag
from pydicom.uid import ExplicitVRLittleEndian
from pydicom.valuerep import DSdecimal, DSfloat, IS

from isocenter import entities, exporters, io_handlers
from isocenter.entities import DicomItem
from isocenter.io_handlers import DicomExporter
from support import plugin_exporters as plugins
from test_the_exporter_writes_the_python_value import (
    _ONE, _OTHER, _PASS, AT_TAG, PRIVATE, SEQ, TEXT_TAGS, _around, _element,
    _merged, _numbers, _run, _through_set_attr)


@pytest.fixture(autouse=True)
def _two_workers(monkeypatch):
    """Every `_run` crosses the recycling pool's pickle; two workers
    start faster than one per core."""
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")


class _MyInt(int):
    pass


class _MyFloat(float):
    pass


class _Code(enum.IntEnum):
    SEVEN = 7


BINARY_TAGS = {"OB": "0042,0011", "OW": "0028,1201", "OF": "0064,0009",
               "OD": "0070,150d", "OL": "0066,0040", "OV": "0072,0081"}
INTEGER_TAGS = {"US": "0028,0010", "SS": "0018,9219", "UL": "0040,a132",
                "SL": "0018,6020", "UV": "0072,0083", "SV": "0072,0082"}
FLOAT_TAGS = {"FL": "0008,9459", "FD": "0018,9087"}
IS_TAG, DS_TAG, US_SS_TAG = "0018,1150", "0028,1050", "0028,0106"
RANGES = {"US": (0, 0xFFFF), "SS": (-0x8000, 0x7FFF),
          "UL": (0, 0xFFFFFFFF), "SL": (-0x80000000, 0x7FFFFFFF),
          "UV": (0, 0xFFFFFFFFFFFFFFFF),
          "SV": (-0x8000000000000000, 0x7FFFFFFFFFFFFFFF)}
PACK = {"US": "H", "SS": "h", "UL": "L", "SL": "l", "UV": "Q", "SV": "q",
        "FL": "f", "FD": "d"}


def test_each_tag_is_the_vr_it_is_listed_under():
    for table in (BINARY_TAGS, INTEGER_TAGS, FLOAT_TAGS):
        for vr, tag in table.items():
            assert pydicom.datadict.dictionary_VR(_numbers(tag)) == vr, tag


def _lost(tag, value, sentence, *, vrs=None):
    """`value` under `tag` loses that element under one STANDARD row
    ending in `sentence`; the dataset writes."""
    raw, losses, notes = _merged({tag: value}, vrs)
    assert raw(tag) is None
    assert losses == [("STANDARD",
                       f"Tag {tag} not exported (data loss): {sentence}")]
    assert notes == []
    return losses[0][1]


# -- T1: a binary VR holds bytes ----------------------------------------------

_NOT_BYTES = [
    pytest.param(lambda: 7, "int", id="int"),
    pytest.param(lambda: 1.5, "float", id="float"),
    pytest.param(lambda: True, "bool", id="bool"),
    # Its twin speaks: `_export_value` makes it the int it equals.
    pytest.param(lambda: np.int64(7), "int", id="numpy-int"),
    pytest.param(lambda: [1, 2], "list", id="list"),
    pytest.param(lambda: "AB", "str", id="str"),
    pytest.param(lambda: Decimal("1.5"), "float", id="Decimal"),
    pytest.param(lambda: np.str_("AB"), "str_", id="numpy-str"),
]

_NUMPY_UNDER_BINARY = [
    pytest.param(lambda: np.datetime64("2020-01-01"), "datetime64",
                 id="datetime64"),
    pytest.param(lambda: np.ma.masked, "MaskedConstant", id="masked"),
    # Owner ruling Q1 (2026-11-22): an array is not bytes either. It was
    # written as its buffer, in its own dtype's width.
    pytest.param(lambda: np.array([7], dtype=np.int64), "ndarray",
                 id="1-d-int64"),
    pytest.param(lambda: np.array([7, 8], dtype=np.uint16), "ndarray",
                 id="1-d-uint16"),
    pytest.param(lambda: np.zeros((2, 2)), "ndarray", id="2-d"),
    pytest.param(lambda: np.ma.array([7, 8], mask=[True, False],
                                     dtype=np.uint16), "MaskedArray",
                 id="1-d-masked"),
]


@pytest.mark.parametrize("vr", sorted(BINARY_TAGS))
@pytest.mark.parametrize("make, kind", _NOT_BYTES)
def test_a_value_a_binary_vr_cannot_hold_loses_that_element(vr, make, kind):
    """#1018: each of these failed the whole file at `dcmwrite`
    (`a bytes-like object is required`)."""
    _lost(BINARY_TAGS[vr], make(),
          f"TypeError: a {kind} is not bytes, and {vr} holds bytes")


@pytest.mark.parametrize("vr", sorted(BINARY_TAGS))
@pytest.mark.parametrize("make, kind", _NUMPY_UNDER_BINARY)
def test_a_numpy_value_under_a_binary_vr_loses_that_element(vr, make, kind):
    """#1000 and owner ruling Q1: each was written as the bytes under it,
    with no row (`np.ma.masked` as its fill, a masked array as the data
    under its mask)."""
    _lost(BINARY_TAGS[vr], make(),
          f"TypeError: a {kind} is not a value {vr} can hold")


# -- T2: an integer or float VR holds what its writer packs -------------------

_NOT_AN_INTEGER = [
    pytest.param(lambda: 1.5, "float", id="float"),
    pytest.param(lambda: 7.0, "float", id="whole-float"),
    pytest.param(lambda: "x", "str", id="str"),
    pytest.param(lambda: "7", "str", id="digits"),
    pytest.param(lambda: b"AB", "bytes", id="bytes"),
    pytest.param(lambda: _MyFloat(1.5), "_MyFloat", id="float-subclass"),
    pytest.param(lambda: [1, "x"], "str", id="list-with-str"),
    pytest.param(lambda: [None, 1], "NoneType", id="list-with-None"),
    pytest.param(lambda: 1e300, "float", id="huge-float"),
    pytest.param(lambda: Decimal("sNaN"), "Decimal", id="sNaN"),
    pytest.param(lambda: 1j, "complex", id="complex"),
]


@pytest.mark.parametrize("vr", sorted(INTEGER_TAGS))
@pytest.mark.parametrize("make, kind", _NOT_AN_INTEGER)
def test_a_value_an_integer_vr_cannot_pack_loses_that_element(vr, make, kind):
    """#986: each failed the whole file (`required argument is not an
    integer`)."""
    detail = _lost(INTEGER_TAGS[vr], make(),
                   f"TypeError: a {kind} is not an integer, and {vr} holds "
                   f"integers")
    assert "1.5" not in detail and "'x'" not in detail


@pytest.mark.parametrize("vr, value", [
    (vr, value) for vr in sorted(INTEGER_TAGS)
    for value in (2 ** 70, RANGES[vr][1] + 1, RANGES[vr][0] - 1)
] + [("US", [1, 70000])])
def test_an_integer_outside_its_vrs_range_loses_that_element(vr, value):
    """#986: `'H' format requires 0 <= number <= 65535`, past `_merge`'s
    `try`, failed the whole file. The row gives the VR's range, which is
    no secret, and never the value."""
    low, high = RANGES[vr]
    detail = _lost(INTEGER_TAGS[vr], value,
                   f"ValueError: an int outside {vr}'s range, {low} to "
                   f"{high}")
    said = detail.split("(data loss): ", 1)[1].split("range")[0]
    for number in (value if isinstance(value, list) else [value]):
        assert str(number) not in said


_NOT_A_NUMBER = [
    pytest.param(lambda: "x", "str", id="str"),
    pytest.param(lambda: "7", "str", id="digits"),
    pytest.param(lambda: b"AB", "bytes", id="bytes"),
    pytest.param(lambda: [1, "x"], "str", id="list-with-str"),
    pytest.param(lambda: [None, 1], "NoneType", id="list-with-None"),
    pytest.param(lambda: Decimal("sNaN"), "Decimal", id="sNaN"),
    pytest.param(lambda: 1j, "complex", id="complex"),
]


@pytest.mark.parametrize("vr", sorted(FLOAT_TAGS))
@pytest.mark.parametrize("make, kind", _NOT_A_NUMBER)
def test_a_value_a_float_vr_cannot_pack_loses_that_element(vr, make, kind):
    """#986: each failed the whole file (`required argument is not a
    float`)."""
    _lost(FLOAT_TAGS[vr], make(),
          f"TypeError: a {kind} is not a number, and {vr} holds numbers")


@pytest.mark.parametrize("vr, value, kind", [
    ("FL", 1e300, "float"), ("FL", [1.5, 1e39], "float"),
    ("FL", 2 ** 2000, "int"), ("FD", 2 ** 2000, "int"),
])
def test_a_number_too_large_for_a_float_vr_loses_that_element(vr, value, kind):
    """`float too large to pack with f format`, and `int too large to
    convert to float`, each failed the whole file."""
    detail = _lost(FLOAT_TAGS[vr], value,
                   f"ValueError: a {kind} too large for {vr}")
    assert "1e+300" not in detail and "39" not in detail


# -- T3: what each VR holds is written as before ------------------------------

def _packed(vr, *numbers):
    return struct.pack(f"<{len(numbers)}{PACK[vr]}", *numbers)


def _controls():
    cells = []
    for vr, tag in INTEGER_TAGS.items():
        low, high = RANGES[vr]
        cells += [
            (tag, 7, (vr, _packed(vr, 7))),
            (tag, True, (vr, _packed(vr, 1))),
            (tag, _MyInt(7), (vr, _packed(vr, 7))),
            (tag, _Code.SEVEN, (vr, _packed(vr, 7))),
            (tag, BaseTag(7), (vr, _packed(vr, 7))),
            (tag, [1, 2], (vr, _packed(vr, 1, 2))),
            (tag, low, (vr, _packed(vr, low))),
            (tag, high, (vr, _packed(vr, high))),
        ]
    for vr in ("SS", "SL", "SV"):
        cells.append((INTEGER_TAGS[vr], -1, (vr, _packed(vr, -1))))
    for vr in ("UV", "SV"):
        cells.append((INTEGER_TAGS[vr], 2 ** 40, (vr, _packed(vr, 2 ** 40))))
    for vr, tag in FLOAT_TAGS.items():
        cells += [
            (tag, 1.5, (vr, _packed(vr, 1.5))),
            (tag, 7, (vr, _packed(vr, 7))),
            (tag, True, (vr, _packed(vr, 1))),
            (tag, _MyFloat(1.5), (vr, _packed(vr, 1.5))),
            (tag, _MyInt(7), (vr, _packed(vr, 7))),
            (tag, DSfloat("1.5"), (vr, _packed(vr, 1.5))),
            (tag, [1.5, 2], (vr, _packed(vr, 1.5, 2))),
            (tag, Decimal("Infinity"), (vr, _packed(vr, float("inf")))),
            (tag, 2 ** 40, (vr, _packed(vr, 2 ** 40))),
        ]
    # A double FL rounds, and an int past 2**24 that FL rounds, are FL's
    # nature, not a loss: written as before.
    cells.append((FLOAT_TAGS["FL"], 16777217, ("FL", _packed("FL", 16777217))))
    cells.append((FLOAT_TAGS["FD"], 2 ** 70, ("FD", _packed("FD", 2 ** 70))))
    for vr, tag in BINARY_TAGS.items():
        cells += [
            (tag, b"ABCDEFGH", (vr, b"ABCDEFGH")),
            (tag, memoryview(b"ABCDEFGH"), (vr, b"ABCDEFGH")),
            (tag, bytearray(b"ABCDEFGH"), (vr, b"ABCDEFGH")),
            (tag, np.bytes_(b"ABCDEFGH"), (vr, b"ABCDEFGH")),
        ]
    return [pytest.param(tag, value, element,
                         id=f"{element[0]}-{type(value).__name__}-{index}")
            for index, (tag, value, element) in enumerate(cells)]


@pytest.mark.parametrize("tag, value, element", _controls())
def test_what_each_vr_holds_is_written_as_before(tag, value, element):
    """The control set. A gate that over-reaches -- refusing `bool`, an
    `IntEnum`, a `BaseTag`, an int under FD, `''` or a `memoryview` --
    is red here. The bytes are what `main` wrote at 3e4ee91a."""
    raw, losses, notes = _merged({tag: value})
    assert raw(tag) == element
    assert losses == [] and notes == []


@pytest.mark.parametrize("vr, tag", sorted(
    {**INTEGER_TAGS, **FLOAT_TAGS, **BINARY_TAGS, "AT": AT_TAG}.items()))
@pytest.mark.parametrize("empty", [None, "", [], b""],
                         ids=["None", "empty-str", "empty-list", "empty-bytes"])
def test_the_zero_length_element_is_written_under_every_vr(vr, tag, empty):
    """What EMPTY writes (`''`), what a removed value reads as (None),
    and `[]`: the zero-length element, as before. No configuration
    reaches the new gates by any other value (the X5 brief, section 0)."""
    ds, losses = Dataset(), []
    DicomExporter._merge(ds, {tag: empty}, losses)
    back = pydicom.dcmread(io.BytesIO(_write(ds)), force=True)
    assert losses == [] and _numbers(tag) in back
    assert back[_numbers(tag)].is_empty and back[_numbers(tag)].VR == vr


# -- T4: a numpy value that is not a number, under any non-text VR ------------

_NUMPY_LEFTOVERS = [
    pytest.param(lambda: np.timedelta64(5, "ns"), "timedelta64",
                 id="timedelta64"),
    pytest.param(lambda: np.complex64(1j), "complex64", id="complex64"),
    pytest.param(lambda: np.ma.masked, "MaskedConstant", id="masked"),
    pytest.param(lambda: np.ma.array(7, mask=True), "MaskedArray",
                 id="masked-0-d"),
    pytest.param(lambda: np.array([7]), "ndarray", id="1-d"),
    pytest.param(lambda: np.zeros((2, 2)), "ndarray", id="2-d"),
    pytest.param(lambda: np.datetime64("2020-01-01"), "datetime64",
                 id="datetime64"),
    pytest.param(lambda: [np.timedelta64(5, "ns")], "timedelta64",
                 id="in-a-list"),
]


@pytest.mark.parametrize("vr, tag", [
    ("IS", IS_TAG), ("DS", DS_TAG), ("FD", FLOAT_TAGS["FD"]),
    ("FL", FLOAT_TAGS["FL"]), ("US", INTEGER_TAGS["US"]),
    ("SV", INTEGER_TAGS["SV"]), ("AT", AT_TAG)])
@pytest.mark.parametrize("make, kind", _NUMPY_LEFTOVERS)
def test_a_numpy_value_that_is_not_a_number_loses_its_element_under_any_vr(
        vr, tag, make, kind):
    """#1000. At 3e4ee91a `timedelta64` was written as its count under IS,
    DS, FD and AT, `complex64` as 0, `np.ma.masked` as `nan` under DS and
    FD, a masked 0-d array as the data under its mask; arrays and
    `datetime64` failed the file under FD and US. `np.longdouble` is not
    here: its `.item()` is platform-dependent."""
    _lost(tag, make(), f"TypeError: a {kind} is not a value {vr} can hold")


@pytest.mark.parametrize("make, sentence", [
    # `timedelta64` is an `Integral` to `numbers`, so `_numeric_arm` lets
    # it by and this gate speaks; it failed the whole file.
    pytest.param(lambda: np.timedelta64(5, "ns"),
                 "TypeError: a timedelta64 is not a value US or SS can hold",
                 id="timedelta64"),
    pytest.param(lambda: [np.timedelta64(5, "ns")],
                 "TypeError: a timedelta64 is not a value US or SS can hold",
                 id="in-a-list"),
    # Everything else `_numeric_arm` refuses first, as it did.
    pytest.param(lambda: np.complex64(1j),
                 "ValueError: the value fits no numeric arm of US or SS",
                 id="complex64"),
    pytest.param(lambda: np.ma.masked,
                 "ValueError: the value fits no numeric arm of US or SS",
                 id="masked"),
    pytest.param(lambda: np.array([7]),
                 "ValueError: the value fits no numeric arm of US or SS",
                 id="1-d"),
    pytest.param(lambda: np.datetime64("2020-01-01"),
                 "ValueError: the value fits no numeric arm of US or SS",
                 id="datetime64"),
])
def test_under_us_or_ss_the_first_refusal_speaks(make, sentence):
    """The ambiguous `US or SS`, resolved after `_merge` by the export's
    own pass (`_resolve_ambiguous_vrs`)."""
    ds, losses, warnings = Dataset(), [], []
    DicomExporter._merge(ds, {US_SS_TAG: make()}, losses)
    io_handlers._resolve_ambiguous_vrs(ds, losses, warnings)
    _write(ds)
    assert US_SS_TAG.replace(",", "") not in [f"{int(e.tag):08x}" for e in ds]
    assert losses == [("STANDARD",
                       f"Tag {US_SS_TAG} not exported (data loss): "
                       f"{sentence}")]


def _write(ds):
    ds.file_meta = FileMetaDataset()
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    buffer = io.BytesIO()
    pydicom.dcmwrite(buffer, ds, enforce_file_format=False)
    return buffer.getvalue()


# -- T5: a file's own IS or DS under a text tag is the file's text ------------

#: The twelve text VRs whose writer could not take pydicom's `IS` or
#: `DSfloat` (DA, DT and TM always wrote its `original_string`), with the
#: VR the source states and the text it holds.
_FILE_NUMBERS = {
    "LO": ("IS", "7", b"7 "), "SH": ("DS", "1.5", b"1.5 "),
    "CS": ("IS", "3", b"3 "), "AS": ("IS", "42", b"42"),
    "AE": ("IS", "12", b"12"), "LT": ("DS", "2.50", b"2.50"),
    "ST": ("IS", "+7", b"+7"), "UT": ("DS", "1e3", b"1e3 "),
    "UC": ("IS", "007", b"007 "), "UR": ("IS", "9", b"9 "),
    "PN": ("IS", "7", b"7 "), "UI": ("DS", "1.5", b"1.5\x00"),
}


@pytest.mark.parametrize("reopen", [False, True], ids=["live", "reopened"])
def test_a_files_own_is_or_ds_under_a_text_tag_is_written_as_the_file_wrote_it(
        tmp_path, reopen):
    """#1017, owner ruling Q3 (all fifteen text VRs). At 3e4ee91a the
    first ten failed the whole file (`object of type 'IS' has no len()`),
    and PN and UI each lost the element in pydicom's words. The file's own
    text is the closest honest output, written under the dictionary's VR
    with no row, as DA, DT and TM already were."""
    def source(given):
        for vr, (stated, text, _written) in _FILE_NUMBERS.items():
            tag = _numbers(TEXT_TAGS[vr])
            given[tag] = pydicom.DataElement(tag, stated, text)

    held = {}

    def look(inst, _item, _session):
        for vr in _FILE_NUMBERS:
            held[vr] = type(inst.attributes[TEXT_TAGS[vr]])

    result = _run(tmp_path / "out", look, reopen=reopen, source=source)
    # The premise: pydicom's own classes, carrying the file's text.
    assert set(held.values()) <= {IS, DSfloat}, held
    assert result["written"] == 1, result["written"]
    for vr, (_stated, _text, written) in _FILE_NUMBERS.items():
        assert _element(result, TEXT_TAGS[vr]) == (vr, written), vr
    assert result["rows"] == [] and result["grade"] == _PASS


@pytest.mark.parametrize("vr", sorted(TEXT_TAGS))
@pytest.mark.parametrize("make, text", [
    pytest.param(lambda: IS("7"), "7", id="IS"),
    pytest.param(lambda: DSfloat("1.50", auto_format=False), "1.50",
                 id="DSfloat"),
    pytest.param(lambda: DSdecimal("1.50"), "1.50", id="DSdecimal"),
    pytest.param(lambda: [IS("1"), DSfloat("2.5")], "1\\2.5", id="list"),
    pytest.param(lambda: MultiValue(IS, ["1", "2"]), "1\\2", id="MultiValue"),
])
def test_a_value_carrying_the_files_text_is_written_as_that_text(
        vr, make, text):
    """`_merge` alone, under every text VR: what is written is exactly
    what the same text as a `str` writes."""
    raw, losses, notes = _merged({TEXT_TAGS[vr]: make()})
    plain, plain_losses, _ = _merged({TEXT_TAGS[vr]: text})
    # Read once each: reading converts the element in place.
    written = raw(TEXT_TAGS[vr])
    assert written is not None and written == plain(TEXT_TAGS[vr])
    assert losses == plain_losses == [] and notes == []


# -- T6: a caller's number subclass under a text VR ---------------------------

@pytest.mark.parametrize("vr", ["LO", "CS", "DA", "SH"])
@pytest.mark.parametrize("make, kind", [
    pytest.param(lambda: _MyInt(7), "_MyInt", id="int-subclass"),
    pytest.param(lambda: _Code.SEVEN, "_Code", id="IntEnum"),
    pytest.param(lambda: _MyFloat(1.5), "_MyFloat", id="float-subclass"),
    # Built in code, so it carries no file's text.
    pytest.param(lambda: IS(7), "IS", id="IS-of-an-int"),
    pytest.param(lambda: DSfloat(1.5), "DSfloat", id="DSfloat-of-a-float"),
    pytest.param(lambda: ["A", _MyInt(7)], "_MyInt", id="in-a-list"),
])
def test_a_callers_number_subclass_under_a_text_vr_loses_that_element(
        vr, make, kind):
    """#986's subclass half. Under LO, SH and CS each failed the whole file
    at `dcmwrite`, while the same store reopened held the plain number and
    lost the one element."""
    _lost(TEXT_TAGS[vr], make(),
          f"TypeError: a {kind} is not text, and {vr} holds text")


# -- T7: a bytearray is the bytes it equals -----------------------------------

@pytest.mark.parametrize("tag", [
    TEXT_TAGS["LO"], TEXT_TAGS["CS"], TEXT_TAGS["DA"], TEXT_TAGS["PN"],
    TEXT_TAGS["UI"], IS_TAG, DS_TAG, BINARY_TAGS["OB"], PRIVATE])
@pytest.mark.parametrize("given", [b"AB", b"\xff\xfe", b"12"])
def test_a_bytearray_is_written_as_the_bytes_it_equals(tag, given):
    """At 3e4ee91a `bytearray(b'AB')` failed the whole file under 13 text
    VRs and was written `65\\66` under IS and DS."""
    as_bytes = _merged({tag: given})
    as_bytearray = _merged({tag: bytearray(given)})
    assert as_bytearray[0](tag) == as_bytes[0](tag)
    assert as_bytearray[1:] == as_bytes[1:]


def test_a_bytearray_in_a_list_is_its_bytes_too():
    as_bytes = _merged({TEXT_TAGS["LO"]: [b"A", b"B"]})
    as_bytearray = _merged({TEXT_TAGS["LO"]: [bytearray(b"A"), b"B"]})
    assert as_bytearray[0](TEXT_TAGS["LO"]) == as_bytes[0](TEXT_TAGS["LO"])
    assert as_bytearray[1:] == as_bytes[1:]


# -- T8: under AT only a tag is written ---------------------------------------

@pytest.mark.parametrize("make, kind", [
    pytest.param(lambda: [1.0, 7], "float", id="float-member"),
    pytest.param(lambda: [1.5, 2.5], "float", id="floats"),
    pytest.param(lambda: 1.5, "float", id="float"),
    pytest.param(lambda: True, "bool", id="bool"),
    pytest.param(lambda: (None, None), "NoneType", id="pair-of-None"),
    pytest.param(lambda: [None, None], "NoneType", id="list-of-None"),
    pytest.param(lambda: (1.0, 7), "float", id="pair-with-a-float"),
    pytest.param(lambda: (Decimal("24"), Decimal("4195")), "float",
                 id="pair-of-Decimals"),
    pytest.param(lambda: (np.float32(1.0), np.float32(7.0)), "float",
                 id="pair-of-numpy-floats"),
    pytest.param(lambda: 1j, "complex", id="complex"),
    pytest.param(lambda: [(None, 7)], "tuple", id="a-pair-holding-None"),
    pytest.param(lambda: [[1, 2, 3]], "list", id="a-3-list-member"),
])
def test_under_at_a_value_that_names_no_tag_loses_that_element(make, kind):
    """#1026. At 3e4ee91a the floats and `True` were written as
    group-0000 tags with no row, and the `None` pairs failed the file."""
    _lost(AT_TAG, make(), f"TypeError: a {kind} is not a tag, and AT holds "
                          f"tags")


def test_under_at_numpy_text_is_the_text_it_is():
    """`np.str_` is a `str`, and pydicom reads `'A'` as the tag
    `(0000,000A)`, as it reads the plain `str` (owner ruling Q4)."""
    for value in ([np.str_("A")], ["A"]):
        raw, losses, _notes = _merged({AT_TAG: value})
        assert raw(AT_TAG) == ("AT", b"\x00\x00\x0a\x00") and losses == []


@pytest.mark.parametrize("value", [-1, 2 ** 32, [7, 2 ** 40]])
def test_under_at_an_int_outside_a_tags_range_loses_that_element(value):
    """Was pydicom's own row, which can quote the value; now the VR's."""
    _lost(AT_TAG, value,
          "ValueError: an int outside AT's range, 0 to 4294967295")


@pytest.mark.parametrize("make, written", [
    pytest.param(lambda: (0x18, 0x1063), _ONE, id="pair"),
    # Owner ruling Q4: a group-0000 tag is a tag, written as it spells.
    pytest.param(lambda: [0x18, 0x1063], b"\x00\x00\x18\x00\x00\x00\x63\x10",
                 id="two-group-0000-tags"),
    pytest.param(lambda: BaseTag(0x00181063), _ONE, id="BaseTag"),
    pytest.param(lambda: "00181063", _ONE, id="hex-text"),
    pytest.param(lambda: [(0x18, 0x1063), (0x18, 0x1064)], _ONE + _OTHER,
                 id="list-of-pairs"),
    pytest.param(lambda: [[0x18, 0x1063]], _ONE, id="list-of-a-2-list"),
    pytest.param(lambda: (True, 7), b"\x01\x00\x07\x00", id="pair-with-a-bool"),
    pytest.param(lambda: 0xFFFFFFFF, b"\xff\xff\xff\xff", id="the-top"),
    # #1009's delta ruling held only what pydicom reads as one tag: a
    # pair of full tags is not one, so it is the list it equals, two tags,
    # where pydicom refused it as one (`OverflowError`) and the element
    # was dropped.
    pytest.param(lambda: (BaseTag(0x00181063), BaseTag(0x00181064)),
                 _ONE + _OTHER, id="pair-of-BaseTags"),
    pytest.param(lambda: (np.uint32(0x00181063), np.uint32(0x00181064)),
                 _ONE + _OTHER, id="pair-of-numpy-full-tags"),
])
def test_under_at_a_tag_is_written_as_before(make, written):
    raw, losses, notes = _merged({AT_TAG: make()})
    assert raw(AT_TAG) == ("AT", written)
    assert losses == [] and notes == []


# -- T9: no cell fails the file -----------------------------------------------

def _every_cell():
    values = [
        7, -1, 1.5, 7.0, True, 2 ** 40, 2 ** 70, 1e300, 1j, "x", "7", "",
        b"AB", b"", bytearray(b"AB"), memoryview(b"ABCDEFGH"), None, [],
        [1, 2], [1, "x"], [None, 1], [None, None], (None, None), (1.0, 7),
        (0x18, 0x1063), [0x18, 0x1063], [1.5, 2.5], [[1]],
        Decimal("1.5"), Decimal("sNaN"), Decimal("Infinity"),
        _MyInt(7), _MyFloat(1.5), _Code.SEVEN,
        IS("7"), DSfloat("1.5"), IS(7), DSfloat(1.5),
        np.int64(7), np.float32(1.5), np.bool_(True), np.str_("A"),
        np.timedelta64(5, "ns"), np.datetime64("2020-01-01"),
        np.complex64(1j), np.ma.masked, np.ma.array(7, mask=True),
        np.array([7]), np.array([1.5, 2.0]), np.zeros((2, 2)),
        np.array([7, 8], dtype=np.uint16), [np.timedelta64(5, "ns")],
        (BaseTag(0x00181063), BaseTag(0x00181064)),
        ["A\\B", "C"], "A\\B",
    ]
    tags = {**BINARY_TAGS, **INTEGER_TAGS, **FLOAT_TAGS, **TEXT_TAGS,
            "IS": IS_TAG, "DS": DS_TAG, "AT": AT_TAG}
    return [pytest.param(tag, index, id=f"{vr}-{index}")
            for vr, tag in sorted(tags.items())
            for index in range(len(values))
            if not (vr in TEXT_TAGS and _not_changed_under_text(
                values[index]))], values


def _not_changed_under_text(value):
    """The cells under a text VR that X5 leaves failing the file, as
    they did at 3e4ee91a, each named in the CHANGELOG's "Not changed": a
    list holding `None` or another container (a caller's shape; no
    source or reopened store holds one), and a `memoryview`, which is
    not `bytes`."""
    if isinstance(value, memoryview):
        return True
    return isinstance(value, (list, tuple)) and any(
        member is None or isinstance(member, (list, tuple))
        for member in value)


_CELLS, _VALUES = _every_cell()


@pytest.mark.parametrize("tag, index", _CELLS)
def test_no_cell_of_the_probe_table_fails_the_file(tag, index):
    """The backstop for the enumerated gate: for every value under every
    VR family, `_merge` and `dcmwrite` never raise. Dozens of these cells
    failed the whole file at 3e4ee91a."""
    _merged({tag: _VALUES[index]})


@pytest.mark.parametrize("index", [
    index for index, value in enumerate(_VALUES)
    if not isinstance(value, memoryview)])
def test_no_cell_fails_the_file_under_us_or_ss(index):
    """A `memoryview` under `US or SS` still fails the file, as it did
    at 3e4ee91a: pydicom resolves the VR and does not convert it, as it
    converts `bytes`. Named under "Not changed"."""
    ds, losses, warnings = Dataset(), [], []
    DicomExporter._merge(ds, {US_SS_TAG: _VALUES[index]}, losses)
    io_handlers._resolve_ambiguous_vrs(ds, losses, warnings)
    _write(ds)


# -- T10: a list member holding a backslash -----------------------------------

def test_a_private_person_names_member_holding_a_backslash_is_a_row():
    """#1014. Under a recorded PN, `['A^B\\\\C^D', 'E^F']` was written as
    three names with no row; a recorded LO already dropped it with a
    PRIVATE row, and now PN does the same."""
    for recorded in ("PN", "LO"):
        raw, losses, _notes = _merged({PRIVATE: ["A^B\\C^D", "E^F"]},
                                      {PRIVATE: recorded})
        assert raw(PRIVATE) is None
        assert losses == [("PRIVATE", f"Tag {PRIVATE} not exported (data "
                                      f"loss): ValueError: no VR fits a list "
                                      f"value")]
    # One member alone follows the list rule.
    raw, losses, _notes = _merged({PRIVATE: ["A^B\\C^D"]}, {PRIVATE: "PN"})
    assert raw(PRIVATE) is None and len(losses) == 1


def test_a_bare_private_person_name_holding_a_backslash_is_still_names():
    """#951: a bare `str` under PN is the caller's multi-value spelling."""
    raw, losses, _notes = _merged({PRIVATE: "A^B\\C^D"}, {PRIVATE: "PN"})
    assert raw(PRIVATE) == ("PN", b"A^B\\C^D ")
    assert losses == []


@pytest.mark.parametrize("vr", ["PN", "LO", "CS", "SH", "UC", "DA", "UI"])
@pytest.mark.parametrize("make", [
    pytest.param(lambda: ["A\\B", "C"], id="list"),
    pytest.param(lambda: ["A\\B"], id="one-member"),
    pytest.param(lambda: ("A\\B", "C"), id="tuple"),
])
def test_a_standard_list_member_holding_a_backslash_is_a_row(vr, make):
    """#1014's standard sibling (owner ruling Q6): three values were
    written where the graph holds two members, with no row."""
    _lost(TEXT_TAGS[vr], make(),
          f"TypeError: a member holding a backslash is several values, and "
          f"{vr} holds one per member")


@pytest.mark.parametrize("tag, value, element", [
    pytest.param(TEXT_TAGS["LO"], "A\\B", ("LO", b"A\\B "), id="bare-str"),
    pytest.param(TEXT_TAGS["LO"], ["A", "B"], ("LO", b"A\\B "), id="list"),
    # Bytes are the wire value (owner ruling Q2) and are not looked into.
    pytest.param(TEXT_TAGS["LO"], [b"A\\B", b"C"], ("LO", b"A\\B\\C "),
                 id="bytes-member"),
    # VM 1: a backslash is text there, and a list is pydicom's own matter.
    pytest.param(TEXT_TAGS["ST"], "A\\B", ("ST", b"A\\B "), id="ST-str"),
    pytest.param(TEXT_TAGS["UT"], "A\\B", ("UT", b"A\\B "), id="UT-str"),
])
def test_what_is_not_a_member_holding_a_backslash_is_written_as_before(
        tag, value, element):
    raw, losses, notes = _merged({tag: value})
    assert raw(tag) == element
    assert losses == [] and notes == []


def test_an_instances_copy_of_a_stamped_tag_holding_a_backslash_has_no_row(
        tmp_path):
    """The owner stamps Patient ID and Patient's Name over the instance's
    copies, so the copy is never what the file carries. Before X5 such a
    list was written and replaced, with no row; the backslash row must
    not appear beside the owner's value (the brief's attack 3)."""
    def edit(inst, _item, _session):
        inst.attributes["0010,0020"] = ["A\\B", "C"]
        inst.attributes["0010,0010"] = ["A^B\\C^D", "E^F"]
        inst.mark_modified()

    result = _run(tmp_path / "out", edit)
    assert result["written"] == 1, result["written"]
    assert result["rows"] == []
    assert _element(result, "0010,0020") is not None
    assert _element(result, "0010,0010") is not None


def test_a_nested_copy_of_a_stamped_tag_holding_a_backslash_keeps_its_row(
        tmp_path):
    """No owner stamps a nested item's Patient ID: its row stays."""
    def edit(_inst, item, _session):
        item.set_attr("0010,0020", ["A\\B", "C"])

    result = _run(tmp_path / "out", edit)
    assert result["written"] == 1, result["written"]
    assert [(a, s, "Tag " + d.split("Tag ", 1)[1])
            for a, s, d in result["rows"]] == [
        ("DATA_LOSS", "STANDARD",
         "Tag 0010,0020 not exported (data loss): TypeError: a member "
         "holding a backslash is several values, and LO holds one per "
         "member")]


# -- T11: one tag saved as a pair is one tag after a reopen --------------------

@pytest.mark.parametrize("route", ["set_attr", "around"])
@pytest.mark.parametrize("nested", [False, True], ids=["top", "nested"])
@pytest.mark.parametrize("reopen", [False, True], ids=["live", "reopened"])
def test_one_tag_set_as_a_pair_is_one_tag_after_a_save_and_a_reopen(
        tmp_path, route, nested, reopen):
    """#1016, owner ruling Q5a. At 3e4ee91a the reopened graph held
    `[24, 4195]` and wrote `(0000,0018)\\(0000,1063)`, with no row and
    PASS. The store now saves the pair as the one tag it names; the live
    graph keeps the tuple (#1173)."""
    pair = (0x0018, 0x1063)
    if route == "set_attr":
        edit = _through_set_attr(AT_TAG, pair, nested=nested)
    else:
        edit = _around(AT_TAG, pair, nested=nested, mark=True)
    held = {}

    def both(inst, item, session):
        edit(inst, item, session)
        held["type"] = type((item if nested else inst).attributes[AT_TAG])

    result = _run(tmp_path / "out", both, reopen=reopen)
    assert held["type"] is tuple
    assert result["written"] == 1, result["written"]
    assert _element(result, AT_TAG, nested) == ("AT", _ONE)
    assert result["rows"] == [] and result["grade"] == _PASS


def test_the_reopened_graph_holds_the_tag_and_the_live_one_the_tuple(
        tmp_path):
    from isocenter.session import DicomSession
    result = _run(tmp_path / "out", _through_set_attr(AT_TAG, (0x18, 0x1063)))
    assert type(result["instance"].attributes[AT_TAG]) is tuple
    session = DicomSession(str(tmp_path / "out" / "s.db"))
    try:
        (patient,) = session.store.patients
        inst = patient.studies[0].series[0].instances[0]
        assert inst.attributes[AT_TAG] == 0x00181063
    finally:
        session.close()


def test_a_list_of_two_ints_is_saved_as_a_list(tmp_path):
    """Owner ruling Q4: a list is two group-0000 tags, saved and reopened
    as it was."""
    result = _run(tmp_path / "out",
                  _through_set_attr(AT_TAG, [0x18, 0x1063]), reopen=True)
    assert _element(result, AT_TAG) == (
        "AT", b"\x00\x00\x18\x00\x00\x00\x63\x10")
    assert result["rows"] == []


def test_the_store_and_the_exporter_ask_one_question():
    """The store's pair test is the exporter's (`entities._one_tag_of`)."""
    for value, tag in [((0x18, 0x1063), 0x00181063), ((True, 7), 0x00010007),
                       (("0018", "1063"), 0x00181063),
                       ((_Code.SEVEN, 1), 0x00070001),
                       ((np.uint16(0x18), np.uint16(0x1063)), 0x00181063)]:
        assert entities._one_tag_of(value) == tag, value
    for value in [[0x18, 0x1063], (0x18,), (1.0, 7), (None, None),
                  (BaseTag(0x00181063), BaseTag(0x00181064)), (-1, 7),
                  (0x10000, 7), ("x", "y"), (0x18, "1063"), 7]:
        assert entities._one_tag_of(value) is None, value


def test_a_private_tag_recorded_at_keeps_its_pair_as_a_list(tmp_path):
    """Owner ruling Q5b: the private recorded-AT pair is filed separately
    and not changed here."""
    def source(given):
        given[0x00111001] = pydicom.DataElement(0x00111001, "AT", 0x00100010)

    result = _run(tmp_path / "out",
                  _through_set_attr(PRIVATE, (0x0018, 0x1063)), reopen=True,
                  source=source)
    assert _element(result, PRIVATE) == (
        "AT", b"\x00\x00\x18\x00\x00\x00\x63\x10")


# -- T12: the US-or-SS arm re-raises a TypeError; the deciders do not ----------

def test_the_us_or_ss_arm_re_raises_a_typeerror(monkeypatch):
    """#972, ruled 2026-10-07: narrow in v1.1. No live value reaches a
    `TypeError` from pydicom's `US or SS` arm any more (the exporter
    hands it Python numbers since #1009, and the numpy gate refuses the
    rest first), so a patched resolver is the only witness. Driven on a
    built `Dataset`: a monkeypatch does not reach an export worker
    process. The waveform arm keeps the unresolved route (next test)."""
    def raising(elem, ds, little_endian, ancestors):
        raise TypeError("patched")

    monkeypatch.setattr(io_handlers, "correct_ambiguous_vr_element", raising)
    ds, losses, warnings = Dataset(), [], []
    ds.add_new(0x00280106, "US or SS", b"\x07\x00")
    io_handlers._resolve_ambiguous_vrs(ds, losses, warnings)
    assert 0x00280106 not in ds
    assert len(losses) == 1 and "TypeError: patched" in losses[0][1]
    # LUT Data keeps #703's arm: unresolved, written `OW`.
    ds, losses, warnings = Dataset(), [], []
    ds.add_new(0x00283006, "US or OW", b"\x07\x00")
    io_handlers._resolve_ambiguous_vrs(ds, losses, warnings)
    assert ds[0x00283006].VR == "OW" and losses == []


@pytest.mark.parametrize("bits", [None, "16"], ids=["none", "text"])
def test_a_waveform_sibling_pydicom_cannot_compare_is_unresolved(bits):
    """X5 review, #972. pydicom's `OB or OW` waveform arm reads
    `WaveformBitsAllocated > 8`, and a sibling holding no value or text
    raises `TypeError` there: the decider failing, as LUT Data's
    subscript does, not a value pydicom could not convert. Both are
    unresolved and written `OW`, as before #972 (`'16'` takes its width
    from `_waveform_bits`). Only the `US or SS` arm, which converts the
    value, re-raises (the test above)."""
    ds, item = Dataset(), Dataset()
    item.add_new(0x54001004, "US", bits)
    item.add_new(0x5400100A, "OB or OW", b"\x00\x80")
    item.add_new(0x54001010, "OB or OW", b"\x01\x00\x02\x00")
    ds.add_new(0x54000100, "SQ", pydicom.sequence.Sequence([item]))
    losses, warnings = [], []
    io_handlers._resolve_ambiguous_vrs(ds, losses, warnings)
    written = ds[0x54000100][0]
    assert losses == []
    assert (written[0x5400100A].VR, written[0x54001010].VR) == ("OW", "OW")


# -- T13: a plain list is handed back without a scan ---------------------------

def test_python_value_hands_a_plain_list_back_without_a_scan():
    """#1004. A plain list was already returned as itself, so this is
    equivalent by output; it pins the set and the one spelling of it."""
    assert {int, float, str, bool, type(None), DSfloat, IS} <= \
        entities._PLAIN_TYPES
    assert Decimal not in entities._PLAIN_TYPES
    assert DSdecimal not in entities._PLAIN_TYPES
    assert not hasattr(io_handlers, "_PLAIN_TYPES")
    big = list(range(65536))
    assert entities._python_value(big) is big
    big.append(np.int64(7))
    converted = entities._python_value(big)
    assert converted is not big and type(converted[-1]) is int
    # A tuple is still scanned (#1173) and stays a tuple.
    pair = (np.uint16(1), 2)
    assert entities._python_value(pair) == (1, 2)
    assert type(entities._python_value(pair)) is tuple


def test_the_gate_asks_a_plain_list_its_types_once(monkeypatch):
    """A 65,536-entry LUT under US costs the gate one pass over its types
    and one `struct.pack`, not a numpy test per member (the per-member
    walk cost about 7 ms there, measured for X5). Equivalent by output:
    no member of a plain list is a numpy value, so the walk never
    raises for one. Pinned by making the per-member test fail loudly;
    a list holding one numpy member still takes it and is refused."""
    def _asked(atom):
        raise AssertionError("the per-member numpy test ran")

    monkeypatch.setattr(io_handlers, "_is_a_numpy_leftover", _asked)
    io_handlers._refuse_what_the_vr_cannot_hold("US", list(range(65536)))
    monkeypatch.undo()
    with pytest.raises(TypeError, match="is not a value US can hold"):
        io_handlers._refuse_what_the_vr_cannot_hold(
            "US", [1, np.timedelta64(5)])


# -- T14: a plugin gets no element its VR cannot hold --------------------------

def test_a_plugin_gets_no_element_its_vr_cannot_hold(tmp_path, monkeypatch):
    """Since #1197 a plugin's `write()` is handed the dataset `_merge`
    built, and nothing writes it afterwards: at 3e4ee91a the plugin got
    `7` under OB. It is not in the dataset now, and its row is."""
    import json
    import os
    import sqlite3
    import shutil
    from pydicom.data import get_testdata_file
    from isocenter.session import DicomSession

    monkeypatch.setenv("ISOCENTER_SHOW_PROGRESS", "0")
    before = dict(exporters._REGISTRY)
    exporters.register("recorder", plugins.Recorder)
    try:
        (tmp_path / "in").mkdir()
        shutil.copy(get_testdata_file("CT_small.dcm"),
                    str(tmp_path / "in" / "a.dcm"))
        db = str(tmp_path / "s.db")
        with DicomSession(db) as session:
            session.ingest(str(tmp_path / "in"))
            (patient,) = session.store.patients
            inst = patient.studies[0].series[0].instances[0]
            inst.set_attr(BINARY_TAGS["OB"], 7)
            session.export(str(tmp_path / "out"), format="recorder")
    finally:
        exporters._REGISTRY.clear()
        exporters._REGISTRY.update(before)
    records = []
    for root, _dirs, names in os.walk(str(tmp_path / "out")):
        for name in names:
            if name.endswith(".json"):
                with open(os.path.join(root, name), encoding="utf-8") as fh:
                    records.append(json.load(fh))
    (record,) = records
    assert "00420011" not in record["tags"]
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT loss_scope, details FROM audit_log WHERE action_type = "
            "'DATA_LOSS'").fetchall()
    assert [(scope, "Tag " + details.split("Tag ", 1)[1])
            for scope, details in rows] == [
        ("STANDARD", "Tag 0042,0011 not exported (data loss): TypeError: a "
                     "int is not bytes, and OB holds bytes")]

"""A private tag keeps its Value Representation across an export (#154).

`DicomExporter._merge` asks `dictionary_VR` for a VR and, for an
odd-group tag, is told there is none -- that is what the standard data
dictionary means. `_fallback_encoding` then picks a VR from the *Python
type* of the value, and every number lands on `('LO', str(value))`.
Measured before the fix, a source block written `SH`, `DS`, `US`, `UL`,
`IS`, `UT`, `FL`, `AT` exported as `LO` for all of them but `UT`, with
`(0009,100f)` -- an `AT` -- coming out as the literal text
`'(0010,0010)'`. The values are byte-faithful and the types are
destroyed, and nothing says so: no exception, no `DATA_LOSS` row, and a
`PASS` grade.

The VR did not survive ingest at all: `attributes` is `{tag: value}`
with nothing beside it. So the fix is a carrier -- `DicomItem
.attribute_vrs` -- and it has to survive three different journeys, which
is why the round trip below is run twice and the parametrisation is over
the whole table rather than one tag:

1. **Fresh export.** `session.export()` is always processes (#185), so
   the carrier is pickled to `_export_instance_worker` with the
   `Instance`.
2. **Reloaded export.** A top-level private tag lives in the
   `instance_attributes` EAV table, whose `value_rep` column was
   reserved for exactly this and hardcoded to `"UN"`.
   `load_vertical_attributes_bulk` also returns text, so a VR alone is
   not enough: pydicom **refuses** a `str` for `US`, `UL` and `FL` at
   write time (`struct.error: required argument is not an integer`), so
   the numeric types have to be restored on load too.
3. **Nested private tags**, which ride the `__sequences__` JSON rather
   than the EAV table and need their own symmetric carrier.

What the recorded VR must never do is bypass the guards in front of it.
#195 (a backslash under a 1-n VR re-splits into two values) and #190 (a
value past a VR's cap) are both properties of the *value*, and
anonymisation can put a value into a private tag that no longer suits
the VR its source element had. The last two tests here are that boundary.
"""
import glob
import os

import numpy as np
import pydicom
import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.filebase import DicomBytesIO
from pydicom.filewriter import write_dataset
from pydicom.multival import MultiValue
from pydicom.tag import Tag
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from isocenter.io_handlers import (
    _FLOAT_VRS,
    _INTEGER_VRS,
    _TEXT_VR_MAX,
    _TEXT_VR_UNCAPPED,
    _value_fits_vr,
    DicomExporter,
)
from isocenter.persistence import _VERTICAL_VR_PARSERS
from isocenter.session import DicomSession

# (element, VR, source value). One row per shape the fallback collapses:
# text that fits LO, text that does not, a decimal string, two unsigned
# integers of different widths, an integer string, a binary float, and a
# tag -- whose `str()` is `'(0010,0010)'`, a spelling nothing can read
# back as a tag.
#
# `UC` is the pair to `UT`, and they differ in the one way that matters
# to the guards: both are uncapped, but `UT` is VM 1 and `UC` is 1-n, so
# a backslash is text in one and a delimiter in the other. It is here
# because an uncapped VR is exactly what a table of caps cannot describe
# by absence -- "not in the cap table" is also what an unknown VR and a
# binary VR look like.
TABLE = [
    (0x1003, 'SH', 'abc'),
    (0x1005, 'DS', 1.5),
    (0x1006, 'US', 7),
    (0x1007, 'UL', 70000),
    (0x100c, 'IS', 42),
    (0x100d, 'UT', 'u' * 90),
    (0x100e, 'FL', 1.25),
    (0x100f, 'AT', 0x00100010),
    (0x1010, 'UC', 'c' * 90),
]


def _write_src(folder):
    """One instance carrying the private block above.

    Explicit VR on purpose: it is what puts a real VR on each private
    element in the source file. Under Implicit VR every one of them
    arrives as `UN`, which is a different population and is
    `tests/test_private_sequence_implicit_vr.py`'s subject.
    """
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.7"
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian

    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "PAT154", "DOE^JOHN"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = meta.MediaStorageSOPClassUID
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "OT", 1, 1
    ds.StudyDate = "20230101"

    ds.add_new(0x00090010, 'LO', 'ACME_HEADER')     # Private Creator
    for element, vr, value in TABLE:
        ds.add_new(Tag(0x0009, element), vr, value)

    # A private element one level down. Nested private tags never reach
    # the EAV tier -- they ride the `__sequences__` JSON -- so they need
    # their own symmetric carrier, and a half-built one makes an inner
    # private tag behave differently from an outer one.
    item = pydicom.Dataset()
    item.add_new(0x00090010, 'LO', 'ACME_HEADER')
    item.add_new(Tag(0x0009, 0x1020), 'US', 9)
    ds.ReferencedImageSequence = pydicom.Sequence([item])

    # Pixel data so the export takes its compressed branch, which writes
    # an explicit-VR transfer syntax (measured: JPEG 2000 Lossless). The
    # uncompressed branch writes Implicit VR Little Endian, under which
    # no private element carries a VR in the file at all and the
    # assertions below could not be made of any implementation.
    ds.Rows = ds.Columns = 4
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit = 7
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelRepresentation = 0
    ds.PixelData = np.zeros((4, 4), dtype=np.uint8).tobytes()

    path = os.path.join(folder, "one.dcm")
    ds.save_as(path, enforce_file_format=True)
    return path


def _export_fresh(tmp_path):
    """Export from the session that ingested, with no store round trip."""
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    _write_src(str(src))
    out = tmp_path / "out"

    session = DicomSession(persistence_file=str(tmp_path / "fresh.db"))
    try:
        session.ingest(str(src))
        session.export(str(out), format="dicom", show_progress=False)
    finally:
        session.close()

    written = glob.glob(str(out / "**" / "*.dcm"), recursive=True)
    assert written, "export produced no .dcm files"
    return pydicom.dcmread(written[0])


def _export_reloaded(tmp_path):
    """Export from a session that opened an existing database."""
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    _write_src(str(src))
    db = str(tmp_path / "reloaded.db")
    out = tmp_path / "out"

    session = DicomSession(persistence_file=db)
    try:
        session.ingest(str(src))
        session.save()
    finally:
        session.close()

    session = DicomSession(persistence_file=db)
    try:
        session.export(str(out), format="dicom", show_progress=False)
    finally:
        session.close()

    written = glob.glob(str(out / "**" / "*.dcm"), recursive=True)
    assert written, "export produced no .dcm files"
    return pydicom.dcmread(written[0])


@pytest.fixture(scope="module")
def _fresh_export(tmp_path_factory):
    return _export_fresh(tmp_path_factory.mktemp("fresh"))


@pytest.fixture(scope="module")
def _reloaded_export(tmp_path_factory):
    return _export_reloaded(tmp_path_factory.mktemp("reloaded"))


@pytest.mark.parametrize("element, vr, value", TABLE)
def test_a_private_tag_exports_under_its_own_vr(_fresh_export, element, vr, value):
    """Red on all eight before the fix: every one but `UT` came out `LO`."""
    exported = _fresh_export[Tag(0x0009, element)]
    assert exported.VR == vr, (
        f"(0009,{element:04x}) was {vr} in the source and exported as "
        f"{exported.VR} carrying {exported.value!r} (#154)")


@pytest.mark.parametrize("element, vr, value", TABLE)
def test_a_private_tag_exports_under_its_own_vr_after_a_reload(
        _reloaded_export, element, vr, value):
    """The half a carrier alone does not fix.

    The EAV tier stores `str(val)` and returns text, and pydicom refuses
    a `str` for `US`, `UL` and `FL` at write time -- so the numeric types
    have to come back with the VR or the export raises rather than
    mislabels.
    """
    exported = _reloaded_export[Tag(0x0009, element)]
    assert exported.VR == vr, (
        f"(0009,{element:04x}) was {vr} in the source and exported as "
        f"{exported.VR} after a save/close/reopen, carrying "
        f"{exported.value!r} (#154)")


def test_the_values_survive_with_the_types(_fresh_export, _reloaded_export):
    """A VR is only worth restoring if the value still means the same thing.

    `AT` is the one that says this out loud: its `str()` is
    `'(0010,0010)'`, which is not a spelling anything reads back as a
    tag, so a fix that recorded the VR and left the text would export a
    conformant-looking `AT` element that is wrong.
    """
    for exported in (_fresh_export, _reloaded_export):
        assert exported[Tag(0x0009, 0x1003)].value == 'abc'
        assert float(exported[Tag(0x0009, 0x1005)].value) == 1.5
        assert exported[Tag(0x0009, 0x1006)].value == 7
        assert exported[Tag(0x0009, 0x1007)].value == 70000
        assert int(exported[Tag(0x0009, 0x100c)].value) == 42
        assert exported[Tag(0x0009, 0x100d)].value == 'u' * 90
        assert exported[Tag(0x0009, 0x100e)].value == pytest.approx(1.25)
        assert exported[Tag(0x0009, 0x100f)].value == Tag(0x0010, 0x0010)


def test_the_private_creator_still_exports_as_LO(_fresh_export, _reloaded_export):
    """Green on both sides: (gggg,0010) was already `LO` by the fallback.

    Here because the recorded VR is `LO` too, so this is the one row
    where the fix and the defect agree -- and if the carrier ever
    recorded the wrong tag's VR, this is where the mismatch shows up
    first.
    """
    for exported in (_fresh_export, _reloaded_export):
        assert exported[Tag(0x0009, 0x0010)].VR == 'LO'
        assert exported[Tag(0x0009, 0x0010)].value == 'ACME_HEADER'


def test_a_private_tag_inside_a_sequence_keeps_its_vr(
        _fresh_export, _reloaded_export):
    """Journey three: the `__sequences__` JSON, not the EAV table.

    Nested values keep their Python type through JSON on their own, so
    this half needs only the VR -- which is exactly why it would have
    been easy to leave out and easy not to notice.
    """
    for exported in (_fresh_export, _reloaded_export):
        item = exported.ReferencedImageSequence[0]
        assert item[Tag(0x0009, 0x1020)].VR == 'US'
        assert item[Tag(0x0009, 0x1020)].value == 9


def test_a_recorded_vr_does_not_bypass_the_backslash_guard():
    """#195's guard runs in front of the recorded VR, not behind it.

    `SH` is a 1-n VR, so a backslash in the value *is* the multiplicity
    on the wire: a value that legitimately contains one comes back split.
    The source element was `SH` and a later edit put a backslash in it,
    so the recorded VR no longer suits the value and the fallback --
    which sends it to `UT`, VM 1 -- has to win.
    """
    ds = pydicom.Dataset()
    DicomExporter._merge(ds, {"0009,1003": "se\\rial"},
                         vrs={"0009,1003": "SH"})
    assert ds[0x00091003].VR == 'UT'
    assert ds[0x00091003].value == "se\\rial"


def test_a_recorded_vr_does_not_bypass_the_length_guard():
    """#190's guard, same rule: `LO` caps at 64 characters."""
    long_value = "x" * 200
    ds = pydicom.Dataset()
    DicomExporter._merge(ds, {"0009,1001": long_value},
                         vrs={"0009,1001": "LO"})
    assert ds[0x00091001].VR == 'UT'
    assert ds[0x00091001].value == long_value


def test_the_range_guard_still_runs_on_the_reloaded_path(tmp_path):
    """The guard where the value is most likely to be out of range.

    Every other guard test here calls `_merge` directly, which is the
    fresh journey. This one is the reloaded one, and it is not a
    duplicate: on the fresh path an out-of-range `US` can only come from
    a `set_attr` still in memory, while on the reloaded path the value
    is *reconstructed* -- `instance_attributes` stores `str(val)` in a
    TEXT column and `_VERTICAL_VR_PARSERS['US']` turns `"70000"` back
    into an `int` -- so the gate is handed a plausible-looking integer
    with no memory of where it came from. Both ends of that composition
    have to hold, and only one of them is `_merge`.

    The assertion is on the *file*, not on the element, because the
    failure this prevents is not a mislabelled element: `add_new`
    accepts 70000 under `US` without a word and `struct.pack` raises
    from `filewriter.write_numbers`, past `_merge`'s `try`, so before
    the range guard this export wrote nothing at all (#154).
    """
    src = tmp_path / "src"
    src.mkdir()
    _write_src(str(src))
    db = str(tmp_path / "range.db")
    out = tmp_path / "out"

    session = DicomSession(persistence_file=db)
    try:
        session.ingest(str(src))
        inst = session.store.patients[0].studies[0].series[0].instances[0]
        assert inst.attribute_vrs["0009,1006"] == 'US'
        # What an anonymisation rule or a hand `set_attr` can do to a
        # perfectly conformant source element: 7 becomes 70000, which is
        # still an `int` and is one past what `US` can encode.
        inst.set_attr("0009,1006", 70000)
        session.save()
    finally:
        session.close()

    session = DicomSession(persistence_file=db)
    try:
        reloaded = session.store.patients[0].studies[0].series[0].instances[0]
        assert reloaded.attributes["0009,1006"] == 70000
        assert isinstance(reloaded.attributes["0009,1006"], int)
        assert reloaded.attribute_vrs["0009,1006"] == 'US'
        session.export(str(out), format="dicom", show_progress=False)
    finally:
        session.close()

    written = glob.glob(str(out / "**" / "*.dcm"), recursive=True)
    assert written, (
        "the export wrote no file: an out-of-range value under a "
        "recorded binary VR reached `struct.pack` and failed the whole "
        "dataset rather than the element (#154)")
    exported = pydicom.dcmread(written[0])
    assert exported[Tag(0x0009, 0x1006)].VR != 'US', (
        "70000 does not fit `US`, so the recorded VR must not be used "
        "for it")
    assert '70000' in str(exported[Tag(0x0009, 0x1006)].value)


def test_a_recorded_vr_does_not_turn_a_bool_into_a_number():
    """The `bool` arm of `_fallback_encoding` was pre-placed for this day.

    `bool` is an `int` subclass, so a private tag whose recorded VR is
    `US` and whose value has since been set to `True` must not be
    written as the integer 1. It takes the fallback, which spells it
    `'True'`.
    """
    ds = pydicom.Dataset()
    DicomExporter._merge(ds, {"0009,1006": True}, vrs={"0009,1006": "US"})
    assert ds[0x00091006].VR == 'LO'
    assert ds[0x00091006].value == 'True'


def test_a_private_binary_value_is_still_written_as_UN():
    """A guard on the fix, not evidence of the defect -- and not, as an
    earlier version of this docstring said, green on both sides. It
    passes `vrs=` to `_merge`, a parameter the unfixed code does not
    accept, so on that side it is a `TypeError` about a signature. An
    unexpected keyword is no more evidence of the defect than the
    `ImportError` the two #183 tests take, and the two have to be
    counted the same way.

    PS3.5 §6.2.2 makes `UN` the VR for an unknown raw-bytes value, and
    `_split_core_and_private` keeps odd-group `bytes` in
    `attributes_json` rather than the EAV table -- so there is no
    `value_rep` home for a binary private tag's VR at all. Nothing is
    recorded for them, deliberately.
    """
    ds = pydicom.Dataset()
    DicomExporter._merge(ds, {"0009,1003": b"\x01\x02\x03\x04"},
                         vrs={"0009,1003": "OB"})
    assert ds[0x00091003].VR == 'UN'
    assert ds[0x00091003].value == b"\x01\x02\x03\x04"


def _merge_and_write(attrs, vrs):
    """Merge, then actually serialise -- the second half is the point.

    Three of the five failures below are invisible to `_merge`:
    `add_new` accepts the value, `_merge` records no loss, and
    `filewriter` raises only when the bytes are written, which fails the
    whole file rather than the element. A test that stops at `_merge`
    cannot see them.
    """
    ds = pydicom.Dataset()
    losses = []
    DicomExporter._merge(ds, attrs, losses, vrs=vrs)
    buf = DicomBytesIO()
    buf.is_little_endian = True
    buf.is_implicit_VR = False
    write_dataset(buf, ds)
    return ds, losses


@pytest.mark.parametrize("vr", ['IS', 'DS'])
def test_a_recorded_vr_does_not_bypass_the_numeric_guard(vr):
    """A value that no longer names a number must take the fallback.

    `IS` and `DS` are text on the wire, so the cap table was the only
    thing the gate asked about them -- and a redaction or an
    anonymisation rule that replaces a vendor `IS` value with a word
    clears a 12-character cap easily. pydicom then raises `ValueError:
    could not convert string to float` from inside `_merge`'s `try`, so
    an element that exported perfectly well as `LO` became a
    `DATA_LOSS` row instead. The gate's own docstring promised a wrong
    answer here is "never worse than no recorded VR at all"; for these
    two VRs it was worse (#154).
    """
    ds, losses = _merge_and_write({"0009,1005": "ANONYMIZED"},
                                  {"0009,1005": vr})
    assert losses == [], losses
    assert ds[0x00091005].VR == 'LO'
    assert ds[0x00091005].value == "ANONYMIZED"


def test_a_recorded_vr_does_not_bypass_the_integer_range_guard():
    """An out-of-range integer fails the FILE, not the element.

    `add_new(tag, 'US', 70000)` is accepted without a word and
    `struct.pack` raises from `filewriter.write_numbers`, past `_merge`'s
    `try`: `OSError: 'H' format requires 0 <= number <= 65535`. So this
    is not a lost tag but a lost export, which is exactly the trap
    `_fallback_encoding`'s docstring names and which a recorded VR must
    not reopen. A source `US` element is always in range; this is the
    value a later `set_attr` put there (#154).
    """
    ds, losses = _merge_and_write({"0009,1006": 70000}, {"0009,1006": "US"})
    assert losses == [], losses
    assert ds[0x00091006].VR == 'LO'
    assert ds[0x00091006].value == '70000'


def test_a_recorded_vr_does_not_bypass_the_float_width_guard():
    """The same, one VR over: `FL` is 32-bit and the value is not.

    `3.5e38` is an ordinary Python float and `struct.pack('<f', ...)`
    cannot carry it -- `OverflowError: float too large to pack with f
    format`, again from `filewriter` rather than from `add_new` (#154).
    """
    ds, losses = _merge_and_write({"0009,100e": 3.5e38},
                                  {"0009,100e": "FL"})
    assert losses == [], losses
    assert ds[0x0009100e].VR == 'LO'
    assert ds[0x0009100e].value == '3.5e+38'


def test_a_recorded_vr_does_not_write_a_number_past_its_cap():
    """#190's cap binds a rendered number too, not only a `str`.

    `_TEXT_VR_MAX` bounds `DS` at 16 characters, and pydicom renders a
    float with `str()` and writes the result without complaint --
    `str(1 / 3)` is 18. The cap was consulted on the `str` arm alone, so
    a recorded `DS` produced a non-conformant element out of a value
    that had exported as a conformant `LO` before (#154, #190).
    """
    ds, losses = _merge_and_write({"0009,1005": 1.0 / 3},
                                  {"0009,1005": "DS"})
    assert losses == [], losses
    assert ds[0x00091005].VR == 'LO'
    assert len(str(ds[0x00091005].value)) == 18


def test_a_recorded_vr_does_not_bypass_the_multi_value_shape_guard():
    """A `tuple` is the one sequence shape `add_new` will not convert.

    pydicom hands back `MultiValue` and the store hands back `list`, so
    a tuple only reaches `attributes` from a hand-written `set_attr` --
    and `_fallback_multivalue` has always accepted one. Under a recorded
    binary VR it reached `struct.pack` still a tuple and failed the
    whole file with "required argument is not an integer" (#154).
    """
    ds, losses = _merge_and_write({"0009,1006": (1, 2)},
                                  {"0009,1006": "US"})
    assert losses == [], losses
    assert ds[0x00091006].VR == 'LO'
    assert list(ds[0x00091006].value) == ['1', '2']


def test_every_binary_vr_the_gate_accepts_has_a_way_back_out_of_the_store():
    """The two tables that decide the reloaded path must name the same VRs.

    `_value_fits_vr` refuses a `str` under any binary VR -- pydicom
    fails the whole export on one -- and the EAV tier stores every value
    stringified, so a binary VR reaches the export as a usable value
    only if `_VERTICAL_VR_PARSERS` reads it back as a number. A VR added
    to `_INTEGER_VRS` or `_FLOAT_VRS` and not to the parsers is not an
    error anywhere: the fresh export keeps the recorded VR, the reloaded
    one silently falls back to `LO`, and the two paths that #154 exists
    to make agree quietly stop agreeing. This is the assertion that
    turns that into a red test (#154).
    """
    assert set(_VERTICAL_VR_PARSERS) == set(_INTEGER_VRS) | set(_FLOAT_VRS)


def test_the_gate_and_the_writer_agree_across_the_whole_vr_table():
    """The property the five tests above are instances of.

    Every VR either table names, against every value shape those tests
    use: whenever `_value_fits_vr` says yes, pydicom must write the
    element under that VR *and* keep every value inside the VR's cap.
    This is the assertion that catches the next VR added to a table
    without its range, parse or cap rule -- which is how `UC`, and then
    `IS`, `DS`, `US` and `FL`, were missed (#154).

    The `accepted` floor is the load-bearing half. The loop only
    asserts on pairings the gate says yes to, so a gate that said no to
    everything -- a typo in a VR name, a table that failed to build, an
    early `return False` -- would run an empty body and report green:
    the strongest test in this file would become the emptiest one.
    Counted on the tables as they stand, 153 of the 26x23 pairings are
    accepted; the floor sits well under that so ordinary table growth
    does not trip it, and well over zero so a collapse does.
    """
    every_vr = sorted(set(_TEXT_VR_MAX) | _TEXT_VR_UNCAPPED
                      | _INTEGER_VRS | _FLOAT_VRS)
    shapes = ["", "AB", "12", "1.5", "nan", "ANONYMIZED", "x" * 65,
              "a\\b", 0, -1, 70000, 2 ** 40, 1.5, 1.0 / 3, 3.5e38,
              float("inf"), True, None, b"\x01", [1, 2], ["AB", "CD"],
              (1, 2), []]

    accepted = 0
    for vr in every_vr:
        for value in shapes:
            if not _value_fits_vr(value, vr):
                continue
            accepted += 1
            ds, _ = _merge_and_write({"0009,1001": value},
                                     {"0009,1001": vr})
            elem = ds[0x00091001]
            assert elem.VR == vr, (vr, value, elem.VR)
            cap = _TEXT_VR_MAX.get(vr)
            if cap is None:
                continue
            atoms = (elem.value if isinstance(elem.value, MultiValue)
                     else [elem.value])
            for atom in atoms:
                assert len(str(atom)) <= cap, (vr, value, atom)

    assert accepted >= 120, (
        f"the gate accepted only {accepted} of the {len(every_vr)}x"
        f"{len(shapes)} pairings; the loop above graded that few, so "
        "its silence is not evidence the gate and the writer agree")

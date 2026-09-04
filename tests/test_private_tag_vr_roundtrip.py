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
from pydicom.tag import Tag
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from isocenter.io_handlers import DicomExporter
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
    """Green on both sides, and it must stay that way.

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

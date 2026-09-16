"""An ambiguous VR the export cannot resolve no longer fails the file
(#674, #675, #681).

Four VRs in the standard name two or three arms -- `US or SS`,
`US or SS or OW`, `US or OW`, `OB or OW` -- and the file that carries one
says which it means through another element. pydicom resolves what it can
at the write; where it cannot, the whole file was lost:

* `AttributeError: Failed to resolve ambiguous VR for tag (5400,0110):
  'Dataset' object has no attribute 'WaveformBitsAllocated'` -- its
  `OB or OW` arm reads the width off the *nearest* dataset, and a Channel
  Definition item never carries it. Every export of an ECG with Channel
  Minimum and Maximum Value failed, both routes, always.
* `ValueError: Cannot write ambiguous VR of 'US or SS' for data element
  with tag (0028,1100)` -- eleven of the thirty-eight ambiguous entries in
  pydicom's dictionaries are outside its correction tables (DICONDE, Curve
  Data, Audio Sample Data, Variable Pixel Data, six retired descriptors).
  Under an explicit-VR syntax those failed the file; under Implicit VR LE
  the byte-valued ones were written by accident, because
  `filewriter.writers` maps the ambiguous *string* to a writer and no VR
  goes on the wire.
* `OSError: With tag (0028,3002) got exception: 'H' format requires
  0 <= number <= 65535` -- pydicom resolved from Pixel Representation and
  the value the source wrote does not fit the arm its own header names.

Every ambiguous VR now gets a concrete arm before `save_as`: pydicom stays
the authority, and where it cannot answer, Pixel Representation, the
declared bit depth and the value decide. The bytes written are the
source's; the arm is what the file declares about them.

Values are read back with `get_item`, which returns the element as the
file holds it -- `Dataset.__getitem__` resolves an ambiguous VR at *read*,
with no ancestors, and that is a second answer to the question under test.
Every expected value is a literal.
"""
import filecmp
import os
import shutil
import sqlite3

import numpy as np
import pydicom
import pytest
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import (ExplicitVRLittleEndian, ImplicitVRLittleEndian,
                         generate_uid)

from isocenter.session import DicomSession

CT_IMAGE = "1.2.840.10008.5.1.4.1.1.2"
CHANNEL_MIN = 0x54000110
CHANNEL_MAX = 0x54000112
WAVEFORM_PADDING = 0x5400100A
WAVEFORM_DATA = 0x54001010
LUT_DESCRIPTOR = 0x00283002
LUT_DATA = 0x00283006
MODALITY_LUT = 0x00283000
GRAY_LUT_DESCRIPTOR = 0x00281100
SMALLEST_PIXEL = 0x00280106
CHANNEL_PATH = ((0x54000100, 0), (0x003A0200, 0))
WAVEFORM_PATH = ((0x54000100, 0),)
LUT_PATH = ((MODALITY_LUT, 0),)


def _dataset(syntax=ExplicitVRLittleEndian, *, pr=0, bits=8):
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = CT_IMAGE
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = syntax
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "PAT674", "DOE^JOHN"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = CT_IMAGE
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "CT", 1, 1
    ds.StudyDate, ds.StudyTime = "20230101", "120000"
    ds.SliceThickness, ds.KVP = "1.0", "120"
    ds.ImagePositionPatient = [0.0, 0.0, 0.0]
    ds.ImageOrientationPatient = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
    ds.PixelSpacing = [1.0, 1.0]
    ds.Rows = ds.Columns = 2
    ds.BitsAllocated = ds.BitsStored = bits
    ds.HighBit = bits - 1
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelRepresentation = pr
    ds.PixelData = bytes(4 * (bits // 8))
    return ds


def _waveform(ds, *, bits=16, declare_bits=True, minmax=True, padding=False):
    """One multiplex group, one channel. `declare_bits` writes Waveform Bits
    Allocated -- the element pydicom resolves the four `OB or OW` waveform
    tags from, and reads off the nearest dataset, which for Channel Minimum
    Value is the channel item, one level below where the width is declared."""
    code = {8: "i1", 16: "i2", 32: "i4"}[bits]
    wf = Dataset()
    wf.MultiplexGroupTimeOffset = "0"
    wf.WaveformOriginality = "ORIGINAL"
    wf.NumberOfWaveformChannels = 1
    wf.NumberOfWaveformSamples = 4
    wf.SamplingFrequency = "500"
    channel = Dataset()
    source = Dataset()
    source.CodeValue = "5.6.3-9-1"
    source.CodingSchemeDesignator = "SCPECG"
    source.CodeMeaning = "Lead I"
    channel.ChannelSourceSequence = Sequence([source])
    channel.WaveformBitsStored = bits
    if minmax:
        channel.add_new(CHANNEL_MIN, "OW", np.array([-100], code).tobytes())
    wf.ChannelDefinitionSequence = Sequence([channel])
    if declare_bits:
        wf.WaveformBitsAllocated = bits
    wf.WaveformSampleInterpretation = "SB" if bits == 8 else "SS"
    if padding:
        wf.add_new(WAVEFORM_PADDING, "OW", np.array([-1], code).tobytes())
    wf.add_new(WAVEFORM_DATA, "OW", np.array([0, 50, -50, 100], code).tobytes())
    ds.WaveformSequence = Sequence([wf])
    return ds


def _lut(ds, *, descriptor=(4, 0, 16), entries=(0, 1000, 40000, 65535),
         nested=True):
    item = Dataset() if nested else ds
    if descriptor is not None:
        item.add_new(LUT_DESCRIPTOR, "SS" if min(descriptor) < 0 else "US",
                     list(descriptor))
    item.add_new(LUT_DATA, "OW", np.array(entries, "<u2").tobytes())
    if nested:
        ds.add_new(MODALITY_LUT, "SQ", Sequence([item]))
    return ds


def _save(tmp_path, ds, syntax, name):
    folder = tmp_path / name
    folder.mkdir()
    pydicom.dcmwrite(str(folder / "one.dcm"), ds,
                     implicit_vr=syntax == ImplicitVRLittleEndian,
                     little_endian=True, force_encoding=True)
    return str(folder)


def _files(folder):
    return sorted(os.path.join(r, f) for r, _d, fs in os.walk(str(folder))
                  for f in fs if f.endswith(".dcm"))


def _instances(session):
    return [i for p in session.store.patients for st in p.studies
            for se in st.series for i in se.instances]


def _rows(db, kind):
    with sqlite3.connect(db) as conn:
        return [r[0] for r in conn.execute(
            "SELECT details FROM audit_log WHERE action_type = ?",
            (kind,)).fetchall()]


def _export(tmp_path, ds, *, syntax=ExplicitVRLittleEndian, compress=False,
            set_attrs=(), out="out"):
    """Ingest one dataset and export it once. Returns (written file, db)."""
    folder = _save(tmp_path, ds, syntax, "src_" + out)
    db = str(tmp_path / (out + ".db"))
    with DicomSession(persistence_file=db) as session:
        assert not session.ingest(folder).failures
        for tag, value in set_attrs:
            _instances(session)[0].set_attr(tag, value)
        result = session.export(str(tmp_path / out), use_compression=compress)
    assert (result.written, _rows(db, "ERROR")) == (1, [])
    (written,) = _files(tmp_path / out)
    return written, db


def _element(path, seq_path, tag):
    """The exported element's VR and raw value, as the file declares them."""
    ds = pydicom.dcmread(path)
    for seq, index in seq_path:
        ds = ds[seq].value[index]
    raw = ds.get_item(tag)
    value = raw.value
    if isinstance(value, (bytes, bytearray, memoryview)):
        value = bytes(value)
    elif isinstance(value, (list, tuple, pydicom.multival.MultiValue)):
        value = list(value)
    return str(raw.VR), value


# --- #674: the deciding sibling is absent -----------------------------------

@pytest.mark.parametrize("bits, vr, value", [
    # 8 bits is one byte, and every element in a DICOM file has an even
    # length: the source's own padding byte is part of the value.
    pytest.param(8, "OB", b"\x9c\x00", id="8-OB"),
    pytest.param(16, "OW", b"\x9c\xff", id="16-OW"),
    pytest.param(32, "OW", b"\x9c\xff\xff\xff", id="32-OW"),
])
def test_a_channel_minimum_takes_its_width_from_the_waveform_item(
        tmp_path, bits, vr, value):
    """(5400,0110) is `OB or OW` and PS3.5 8.3 decides by the bit depth --
    which the multiplex group declares, one level above the channel item
    pydicom looks in. Every export of an ECG carrying it failed here."""
    ds = _waveform(_dataset(), bits=bits)
    written, db = _export(tmp_path, ds, compress=True)
    assert _element(written, CHANNEL_PATH, CHANNEL_MIN) == (vr, value)
    # The file said what the width is, one level up: nothing is in doubt.
    assert _rows(db, "WARNING") == []


def test_a_waveform_item_with_no_bits_allocated_is_written_and_named(tmp_path):
    """Waveform Bits Allocated is Type 1 and this source omits it: the bytes
    are written as words, and one row names every element that cost."""
    ds = _waveform(_dataset(), declare_bits=False, minmax=False, padding=True)
    written, db = _export(tmp_path, ds, compress=True)
    assert _element(written, WAVEFORM_PATH, WAVEFORM_DATA) == (
        "OW", b"\x00\x00\x32\x00\xce\xff\x64\x00")
    assert _element(written, WAVEFORM_PATH, WAVEFORM_PADDING) == (
        "OW", b"\xff\xff")
    assert _rows(db, "WARNING") == [
        "Ambiguous value representations (5400,100a): no Waveform Bits "
        "Allocated is declared anywhere above it, and PS3.5 8.3 decides "
        "between OB and OW by the bit depth, so OW was written; (5400,1010): "
        "no Waveform Bits Allocated is declared anywhere above it, and PS3.5 "
        "8.3 decides between OB and OW by the bit depth, so OW was written. "
        "The written bytes are the source's; what this library had to choose "
        "is the value representation the file declares, which decides how a "
        "reader interprets those bytes."]


def test_one_row_names_ten_elements_and_counts_the_rest(tmp_path):
    """A 12-channel ECG whose multiplex group never declared its bit depth
    carries 25 elements this pass had to choose an arm for -- 24 channel
    minima and maxima and the waveform itself -- and one row is still one
    sentence: the first ten are named and the rest are counted, the shape
    `_AMBIGUOUS_NAMED` exists for."""
    ds = _dataset()
    wf = Dataset()
    wf.MultiplexGroupTimeOffset = "0"
    wf.WaveformOriginality = "ORIGINAL"
    wf.NumberOfWaveformChannels = 12
    wf.NumberOfWaveformSamples = 4
    wf.SamplingFrequency = "500"
    wf.WaveformSampleInterpretation = "SS"
    channels = []
    for lead in range(12):
        channel = Dataset()
        source = Dataset()
        source.CodeValue = "5.6.3-9-1"
        source.CodingSchemeDesignator = "SCPECG"
        source.CodeMeaning = f"Lead {lead + 1}"
        channel.ChannelSourceSequence = Sequence([source])
        channel.WaveformBitsStored = 16
        channel.add_new(CHANNEL_MIN, "OW", np.array([-100], "i2").tobytes())
        channel.add_new(CHANNEL_MAX, "OW", np.array([100], "i2").tobytes())
        channels.append(channel)
    wf.ChannelDefinitionSequence = Sequence(channels)
    wf.add_new(WAVEFORM_DATA, "OW", np.array([0] * 48, "i2").tobytes())
    ds.WaveformSequence = Sequence([wf])

    written, db = _export(tmp_path, ds, compress=True)
    assert _element(written, CHANNEL_PATH, CHANNEL_MIN)[0] == "OW"
    (row,) = _rows(db, "WARNING")
    assert row.count("no Waveform Bits Allocated is declared") == 10
    assert "; and 15 more. The written bytes are the source's;" in row


def test_the_row_is_one_per_instance_not_one_per_file(tmp_path):
    """Two instances in one export, each missing the LUT Descriptor its LUT
    Data needs: two rows with the same words, because each states a fact
    about one file that was written."""
    folder = tmp_path / "src_two"
    folder.mkdir()
    for n in range(2):
        ds = _lut(_dataset(), descriptor=None)
        ds.InstanceNumber = n + 1
        pydicom.dcmwrite(str(folder / f"{n}.dcm"), ds, implicit_vr=False,
                         little_endian=True, force_encoding=True)
    db = str(tmp_path / "two.db")
    with DicomSession(persistence_file=db) as session:
        assert not session.ingest(str(folder)).failures
        result = session.export(str(tmp_path / "two_out"), use_compression=True)
    assert (result.written, _rows(db, "ERROR")) == (2, [])
    rows = _rows(db, "WARNING")
    assert len(rows) == 2
    assert set(rows) == {
        "Ambiguous value representation (0028,3006): the LUT it belongs to "
        "declares no LUT Descriptor, whose first value decides between US and "
        "OW, so OW was written. The written bytes are the source's; what this "
        "library had to choose is the value representation the file declares, "
        "which decides how a reader interprets those bytes."}


@pytest.mark.parametrize("nested", [True, False], ids=["nested", "top-level"])
def test_lut_data_with_no_descriptor_is_written_as_words(tmp_path, nested):
    """LUT Descriptor is Type 1 and its first value decides `US or OW`."""
    ds = _lut(_dataset(), descriptor=None, nested=nested)
    written, db = _export(tmp_path, ds, compress=True)
    assert _element(written, LUT_PATH if nested else (), LUT_DATA) == (
        "OW", b"\x00\x00\xe8\x03@\x9c\xff\xff")
    assert _rows(db, "WARNING") == [
        "Ambiguous value representation (0028,3006): the LUT it belongs to "
        "declares no LUT Descriptor, whose first value decides between US and "
        "OW, so OW was written. The written bytes are the source's; what this "
        "library had to choose is the value representation the file declares, "
        "which decides how a reader interprets those bytes."]


# --- #674's comment: the tags pydicom's tables leave out --------------------

@pytest.mark.parametrize("tag, value", [
    pytest.param(0x00143050, b"\x01\x00\x02\x00\x03\x00\x04\x00",
                 id="diconde-dark-current-counts"),
    pytest.param(0x00143070, b"\x01\x02\x03\x00", id="diconde-air-counts"),
    pytest.param(0x50003000, b"\x00\x00\xe8\x03", id="curve-data-group-5000"),
    pytest.param(0x50023000, b"\x00\x00\xe8\x03", id="curve-data-group-5002"),
    pytest.param(0x5000200C, b"\x00\x00\x18\xfc", id="audio-sample-data"),
    pytest.param(0x7F000010, b"\x01\x00\x02\x00", id="variable-pixel-data"),
    # (0028,1200) Gray LUT Data is `US or SS or OW`, so bytes under it take
    # the same `OW` as DICONDE does -- and its numeric half is already
    # `_numeric_arm`'s (#653), which is why only this half is new.
    pytest.param(0x00281200, b"\x00\x00\xe8\x03", id="gray-lut-data-bytes"),
])
def test_a_tag_outside_pydicoms_tables_is_written_under_both_routes(
        tmp_path, tag, value):
    """`Cannot write ambiguous VR of 'OB or OW'` failed every explicit-VR
    export of these. The standard names no decider for them, so nothing
    about the source is wrong and no row is owed; only the bytes matter,
    and they are the source's on both routes."""
    ds = _dataset()
    ds.add_new(tag, "OW", value)
    if tag in (0x50003000, 0x50023000):
        ds.add_new(tag & 0xFFFF0000 | 0x0103, "US", 0)   # Data Value Repn
    j2k, db = _export(tmp_path, ds, compress=True, out="j2k")
    assert _element(j2k, (), tag) == ("OW", value)
    assert _rows(db, "WARNING") == []

    native, _db = _export(tmp_path, ds, compress=False, out="native")
    # Implicit VR LE carries no VR, so the bytes are the whole of what the
    # file says about this element, and they are the source's.
    assert _element(native, (), tag)[1] == value


# --- #675: the retired and omitted `US or SS` descriptors -------------------

@pytest.mark.parametrize("tag, source_vr, value, raw", [
    pytest.param(GRAY_LUT_DESCRIPTOR, "US", [4, 0, 16],
                 b"\x04\x00\x00\x00\x10\x00", id="gray-lut-descriptor"),
    pytest.param(0x00280071, "US", 7, b"\x07\x00", id="perimeter-value"),
    pytest.param(0x00281111, "US", [4, 0, 16], b"\x04\x00\x00\x00\x10\x00",
                 id="large-red-palette-descriptor"),
    pytest.param(0x00281112, "US", [4, 0, 16], b"\x04\x00\x00\x00\x10\x00",
                 id="large-green-palette-descriptor"),
    pytest.param(0x00281113, "US", [4, 0, 16], b"\x04\x00\x00\x00\x10\x00",
                 id="large-blue-palette-descriptor"),
])
@pytest.mark.parametrize("syntax", [ExplicitVRLittleEndian,
                                    ImplicitVRLittleEndian],
                         ids=["explicit-source", "implicit-source"])
def test_a_descriptor_outside_pydicoms_tables_takes_the_pixel_representation_arm(
        tmp_path, tag, source_vr, value, raw, syntax):
    """`US or SS` has no bytes arm, so the omitted tags could not fall
    through to a writer the way `OB or OW` did: j2k raised `Cannot write
    ambiguous VR of 'US or SS'`, and the native route raised `TypeError: a
    bytes-like object is required, not 'MultiValue'`. Pixel Representation
    is the rule pydicom applies to their non-retired twins, and it does not
    matter which syntax the source was written in: an Implicit VR source
    hands these back as bytes and an Explicit VR one as numbers, and both
    mean the same element."""
    ds = _dataset(syntax, pr=0)
    ds.add_new(tag, source_vr, value)
    written, db = _export(tmp_path, ds, syntax=syntax, compress=True)
    assert _element(written, (), tag) == ("US", raw)
    assert _rows(db, "WARNING") == []


@pytest.mark.parametrize("tag, source_vr, raw", [
    pytest.param(LUT_DESCRIPTOR, "SS", b"\x04\x00\x10\x00\x10\x00",
                 id="in-pydicoms-table"),
    pytest.param(GRAY_LUT_DESCRIPTOR, "SS", b"\x04\x00\x10\x00\x10\x00",
                 id="outside-pydicoms-table"),
])
def test_a_descriptor_under_pixel_representation_one_is_written_signed(
        tmp_path, tag, source_vr, raw):
    """The guard on the arm above, and the one test that fails if the choice
    is made from the value rather than the header: `[4, 16, 16]` fits `US`
    too, and Pixel Representation 1 still means `SS`."""
    ds = _dataset(pr=1)
    ds.add_new(tag, source_vr, [4, 16, 16])
    written, db = _export(tmp_path, ds, compress=True)
    assert _element(written, (), tag) == ("SS", raw)
    assert _rows(db, "WARNING") == []


# --- #681: the arm the header names cannot hold the value -------------------

@pytest.mark.parametrize(
    "tag, seq_path, pr, source_vr, values, raw, vr, named", [
        pytest.param(LUT_DESCRIPTOR, LUT_PATH, 0, "SS", [4, -2048, 16],
                     b"\x04\x00\x00\xf8\x10\x00", "SS", "US",
                     id="negative-under-PR-0"),
        pytest.param(LUT_DESCRIPTOR, LUT_PATH, 1, "US", [4, 40000, 16],
                     b"\x04\x00@\x9c\x10\x00", "US", "SS",
                     id="over-32767-under-PR-1"),
        # A tag pydicom's tables omit takes the same row by the other
        # route: pydicom names no arm at all, so the arm and the veto are
        # decided in one place here rather than pydicom's answer being
        # refused. Without this case that branch's row is written by
        # nothing under test, and a caller loses the clause with every
        # end-to-end test still green.
        pytest.param(GRAY_LUT_DESCRIPTOR, (), 0, "SS", [4, -2048, 16],
                     b"\x04\x00\x00\xf8\x10\x00", "SS", "US",
                     id="omitted-tag-negative-under-PR-0"),
        # And the mirror, because one direction pins only half of the
        # clause: with `named` hardcoded to `US` the case above still
        # passes, and a caller with this source reads "written US, Pixel
        # Representation names US and the value needs US".
        pytest.param(GRAY_LUT_DESCRIPTOR, (), 1, "US", [4, 40000, 16],
                     b"\x04\x00@\x9c\x10\x00", "US", "SS",
                     id="omitted-tag-over-32767-under-PR-1"),
    ])
def test_a_descriptor_the_pixel_representation_cannot_hold_is_written_otherwise(
        tmp_path, tag, seq_path, pr, source_vr, values, raw, vr, named):
    """A source contradicting its own header cost the whole file:
    `'H' format requires 0 <= number <= 65535`, raised inside `dcmwrite`."""
    ds = _dataset(pr=pr)
    if seq_path:
        item = Dataset()
        item.add_new(tag, source_vr, values)
        item.add_new(LUT_DATA, "OW", bytes(8))
        ds.add_new(MODALITY_LUT, "SQ", Sequence([item]))
    else:
        ds.add_new(tag, source_vr, values)
    written, db = _export(tmp_path, ds, compress=True)
    named_tag = f"{tag >> 16:04x},{tag & 0xFFFF:04x}"
    assert _element(written, seq_path, tag) == (vr, raw)
    assert _rows(db, "WARNING") == [
        f"Ambiguous value representation ({named_tag}) written {vr}, Pixel "
        f"Representation names {named} and the value needs {vr}. The written "
        f"bytes are the source's; what this library had to choose is the "
        f"value representation the file declares, which decides how a reader "
        f"interprets those bytes."]


def test_a_smallest_image_pixel_value_below_zero_is_written_signed(tmp_path):
    """(0028,0106) is `US or SS`, Type 3, and on an ordinary image: the same
    contradiction with no LUT anywhere."""
    ds = _dataset(pr=0)
    ds.add_new(SMALLEST_PIXEL, "SS", -5)
    written, db = _export(tmp_path, ds, compress=True)
    assert _element(written, (), SMALLEST_PIXEL) == ("SS", b"\xfb\xff")
    assert len(_rows(db, "WARNING")) == 1


# --- the values no arm holds ------------------------------------------------

@pytest.mark.parametrize("tag, value, detail", [
    pytest.param("0028,3002", [4, 70000, 16],
                 "Tag 0028,3002 not exported (data loss): ValueError: the "
                 "value fits no numeric arm of US or SS", id="US-or-SS-over-65535"),
    pytest.param("0028,3002", [4, -40000, 16],
                 "Tag 0028,3002 not exported (data loss): ValueError: the "
                 "value fits no numeric arm of US or SS", id="US-or-SS-under--32768"),
    pytest.param("0028,3002", [1.5, 2.5],
                 "Tag 0028,3002 not exported (data loss): ValueError: the "
                 "value fits no numeric arm of US or SS", id="US-or-SS-not-integers"),
    pytest.param("0028,3006", [0, 70000],
                 "Tag 0028,3006 not exported (data loss): ValueError: the "
                 "value fits no numeric arm of US or OW, and OW holds only "
                 "bytes", id="US-or-OW-over-65535"),
    pytest.param("0014,3050", [1, 2],
                 "Tag 0014,3050 not exported (data loss): ValueError: the "
                 "value fits no numeric arm of OB or OW, and OW holds only "
                 "bytes", id="OB-or-OW-numbers"),
    pytest.param("5000,3000", "not bytes",
                 "Tag 5000,3000 not exported (data loss): ValueError: the "
                 "value fits no numeric arm of OB or OW, and OW holds only "
                 "bytes", id="OB-or-OW-text"),
])
def test_a_value_no_arm_holds_is_one_elements_loss(tmp_path, tag, value, detail):
    """A caller's `set_attr` of a value the arms cannot express: the file is
    still written, and the element that could not be placed is one
    `DATA_LOSS` row rather than the whole file's export failure."""
    written, db = _export(tmp_path, _dataset(), compress=True,
                          set_attrs=[(tag, value)])
    assert _rows(db, "DATA_LOSS") == [detail]
    assert int(tag.replace(",", ""), 16) not in pydicom.dcmread(written)


def test_the_veto_drops_a_value_neither_arm_holds():
    """The veto's fall-through, asserted by calling it directly.

    Nothing reaches it through the pipeline -- `_merge`'s widened
    `_numeric_arm` refuses a value no arm holds first, which is the case
    above -- so the line that drops the element is here because the two
    halves of one rule must be spelled the same way, not because a source
    can trip it. Kept undefended it would be the one path left inside this
    fix that can still fail a whole file: an element written under an arm
    its value overflows raises `OSError` from `dcmwrite`."""
    from isocenter.io_handlers import _veto_ambiguous_arm

    ds = Dataset()
    ds.add_new(SMALLEST_PIXEL, "US or SS", [70000])
    ds[SMALLEST_PIXEL].VR = "US"            # the arm pydicom would name
    losses, rows = [], []
    _veto_ambiguous_arm(ds[SMALLEST_PIXEL], ds, ["US", "SS"], losses, rows,
                        "US")
    assert SMALLEST_PIXEL not in ds
    assert (rows, losses) == ([], [
        ("STANDARD", "Tag (0028,0106) not exported (data loss): its value "
                     "fits no numeric arm of US or SS.")])


# --- the postcondition, the round trip, and what is left alone -------------

def test_no_exported_element_keeps_an_ambiguous_value_representation(tmp_path):
    """The property `save_as` needs, over one dataset carrying every shape at
    once. Asserted on the explicit-VR route, the only one that writes a VR at
    all."""
    from pydicom.filewriter import AMBIGUOUS_VR

    ds = _lut(_waveform(_dataset(pr=0), declare_bits=False), descriptor=None)
    ds.add_new(0x00143050, "OW", b"\x01\x00\x02\x00")
    ds.add_new(GRAY_LUT_DESCRIPTOR, "US", [4, 0, 16])
    ds.add_new(SMALLEST_PIXEL, "SS", -5)
    written, _db = _export(tmp_path, ds, compress=True)

    def ambiguous(dataset):
        for element in dataset:
            if element.VR == "SQ":
                for item in element.value:
                    yield from ambiguous(item)
            elif str(dataset.get_item(element.tag).VR) in AMBIGUOUS_VR:
                yield element.tag

    assert list(ambiguous(pydicom.dcmread(written))) == []


@pytest.mark.parametrize("build, compress", [
    (lambda: _waveform(_dataset(), bits=16), True),
    (lambda: _lut(_dataset(), descriptor=None), False),
], ids=["waveform-j2k", "descriptorless-lut-native"])
def test_an_export_of_an_export_is_byte_identical(tmp_path, build, compress):
    """Our own output re-ingests and re-exports to the same bytes: the arm
    chosen at the first write is the arm read back at the second.

    The native descriptorless LUT was the one exception until #691: that
    export is Implicit VR LE, which carries no arm, and pydicom's read-time
    resolution refused the file. Ingest now resolves it by this same rule
    (`io_handlers._read_element`)."""
    first, _db = _export(tmp_path, build(), compress=compress, out="first")
    again = tmp_path / "again"
    again.mkdir()
    shutil.copy(first, str(again / "two.dcm"))
    with DicomSession(persistence_file=str(tmp_path / "b.db")) as session:
        assert not session.ingest(str(again)).failures
        assert session.export(str(tmp_path / "second"),
                              use_compression=compress).written == 1
    (second,) = _files(tmp_path / "second")
    assert filecmp.cmp(first, second, shallow=False)


@pytest.mark.parametrize("bits, vr", [(8, "OB"), (16, "OB")])
def test_pixel_data_keeps_the_answer_pydicom_gives_it(tmp_path, bits, vr):
    """(7FE0,0010) is `OB or OW` too, and it is the one ambiguous VR this
    must not answer: PS3.5 A.4's `OB` for an encapsulated stream follows
    from the undefined length `save_as` gives it, which is not yet true
    where the pass runs -- there the element has a defined length and an
    `original_encoding` of (True, True), and pydicom's Implicit VR arm
    answers `OW`. Asking early turned every compressed export's `OB` into
    `OW`. This test passes before the fix as well as after; it is the
    guard that says so."""
    ds = _dataset(bits=bits)
    pixels = np.array([0, 1, 2, 3], f"<u{bits // 8}").tobytes()
    ds.PixelData = pixels
    j2k, _db = _export(tmp_path, ds, compress=True, out="j2k")
    assert _element(j2k, (), 0x7FE00010)[0] == vr
    native, _db2 = _export(tmp_path, ds, compress=False, out="native")
    assert _element(native, (), 0x7FE00010)[1] == pixels


def test_pixel_representation_is_the_nearest_ancestor_that_declares_one():
    """The rule pydicom's `US or SS` arm walks, spelled out for the tags its
    table omits: nearest first, root last, and None when no one says.

    Asserted directly because the pipeline cannot separate the arms of it:
    a sequence item in an exported dataset carries pydicom's own propagated
    `_pixel_rep`, so a nearest-only walk answers the same for every file
    that can be built (the mutant that shortens this walk survives every
    end-to-end test)."""
    from isocenter.io_handlers import _pixel_representation

    root = Dataset()
    root.PixelRepresentation = 1
    unsigned_item, bare = Dataset(), Dataset()
    unsigned_item.PixelRepresentation = 0
    assert _pixel_representation([bare, root]) == 1
    assert _pixel_representation([unsigned_item, root]) == 0
    assert _pixel_representation([root]) == 1
    # None, not 0: callers take unsigned either way, and only the row that
    # reports the choice needs to know the source declared nothing.
    assert _pixel_representation([bare]) is None
    propagated = Dataset()
    propagated._pixel_rep = 1
    assert _pixel_representation([propagated]) == 1


def test_a_nested_descriptor_takes_the_pixel_representation_above_it(tmp_path):
    """Pixel Representation is declared once, at the top, and an element
    inside a sequence item is decided by it: the walk is nearest-first, not
    nearest-only. `[4, 16, 16]` fits both arms, so only the header can
    choose, and for a tag pydicom's table omits the choice is ours."""
    ds = _dataset(pr=1)
    item = Dataset()
    item.add_new(GRAY_LUT_DESCRIPTOR, "SS", [4, 16, 16])
    item.add_new(LUT_DATA, "OW", bytes(8))
    item.add_new(LUT_DESCRIPTOR, "SS", [4, 16, 16])
    ds.add_new(MODALITY_LUT, "SQ", Sequence([item]))
    written, db = _export(tmp_path, ds, compress=True)
    assert _element(written, LUT_PATH, GRAY_LUT_DESCRIPTOR) == (
        "SS", b"\x04\x00\x10\x00\x10\x00")
    assert _rows(db, "WARNING") == []


def test_an_empty_ambiguous_element_is_written_empty(tmp_path):
    """A `US or SS` element with no value at all contradicts nothing: it is
    written empty, with no row and no loss. The arms differ only in how a
    reader reads bytes, and here there are none."""
    ds = _dataset(pr=0)
    ds.add_new(SMALLEST_PIXEL, "US", None)
    written, db = _export(tmp_path, ds, compress=True)
    assert _element(written, (), SMALLEST_PIXEL) == ("US", None)
    assert (_rows(db, "WARNING"), _rows(db, "DATA_LOSS")) == ([], [])


def test_a_descriptor_whose_bytes_exceed_32767_stays_unsigned(tmp_path):
    """An Implicit VR source hands `US or SS` back as bytes, and the arm
    Pixel Representation names decides how they are read: 40000 under Pixel
    Representation 0 is 40000, not -25536, and nothing in the source
    contradicts its own header."""
    ds = _dataset(ImplicitVRLittleEndian, pr=0)
    ds.add_new(GRAY_LUT_DESCRIPTOR, "US", [4, 40000, 16])
    written, db = _export(tmp_path, ds, syntax=ImplicitVRLittleEndian,
                          compress=True)
    assert _element(written, (), GRAY_LUT_DESCRIPTOR) == (
        "US", b"\x04\x00@\x9c\x10\x00")
    assert _rows(db, "WARNING") == []


def _pixel_less(ds):
    """The same dataset as an ECG: no Pixel Data and no image descriptors,
    so nothing in it declares a Pixel Representation."""
    for tag in (0x7FE00010, 0x00280103, 0x00280100, 0x00280101, 0x00280102,
                0x00280010, 0x00280011, 0x00280002, 0x00280004):
        if tag in ds:
            del ds[tag]
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.9.1.1"              # 12-lead ECG
    ds.file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
    ds.Modality = "ECG"
    return ds


@pytest.mark.parametrize("tag, raw", [
    # The omitted tag reaches the numbers branch (pydicom answers nothing
    # for it); the tabulated one reaches the veto (pydicom's `US or SS`
    # arm answers `US` for a dataset with neither the element nor pixels,
    # without raising). Both sites must say the same true thing.
    pytest.param(GRAY_LUT_DESCRIPTOR, b"\x04\x00\x00\xf8\x10\x00",
                 id="omitted-tag"),
    pytest.param(SMALLEST_PIXEL, b"\xfb\xff", id="tabulated-tag"),
])
def test_a_row_about_an_undeclared_pixel_representation_says_so(
        tmp_path, tag, raw):
    """A `WARNING` row is read as a fact about the caller's data, so it may
    not name an element the file does not contain. A pixel-less instance
    declares no Pixel Representation anywhere: the unsigned arm is this
    library's default there -- pydicom's own answer for a dataset with
    neither the element nor pixels -- and where the value cannot fit it,
    the row says that, rather than "Pixel Representation names US" about a
    header nothing wrote."""
    ds = _pixel_less(_waveform(_dataset(), bits=16))
    ds.add_new(tag, "SS", [4, -2048, 16] if tag == GRAY_LUT_DESCRIPTOR else -5)
    written, db = _export(tmp_path, ds, compress=False)
    named_tag = f"{tag >> 16:04x},{tag & 0xFFFF:04x}"
    assert _element(written, (), tag)[1] == raw
    assert _rows(db, "WARNING") == [
        f"Ambiguous value representation ({named_tag}): no Pixel "
        f"Representation is declared anywhere above it, and the unsigned "
        f"arm it defaults to cannot hold the value, so SS was written. The "
        f"written bytes are the source's; what this library had to choose "
        f"is the value representation the file declares, which decides how "
        f"a reader interprets those bytes."]


def test_a_waveform_only_instance_takes_the_unsigned_arm(tmp_path):
    """No Pixel Data and no Pixel Representation anywhere: pydicom's own
    rule ends at `US` for a dataset that has neither, and a waveform
    instance carrying a retired descriptor is what reaches that branch.

    The arm is asserted twice, and the second half is the one with teeth: a
    pixel-less instance can only take the native route, Implicit VR LE puts
    no VR on the wire, and `[4, 0, 16]` writes the same six bytes under `US`
    as under `SS` -- so end to end this shape can only say the export did
    not fail. The arm itself is asserted where it is chosen."""
    ds = _pixel_less(_waveform(_dataset(), bits=16))
    ds.add_new(GRAY_LUT_DESCRIPTOR, "US", [4, 0, 16])
    written, db = _export(tmp_path, ds, compress=False)
    assert _element(written, (), GRAY_LUT_DESCRIPTOR)[1] == \
        b"\x04\x00\x00\x00\x10\x00"
    assert _rows(db, "WARNING") == []

    from isocenter.io_handlers import _resolve_ambiguous_vrs

    bare = Dataset()
    bare.add_new(GRAY_LUT_DESCRIPTOR, "US or SS",
                 b"\x04\x00\x00\x00\x10\x00")
    _resolve_ambiguous_vrs(bare, [], [])
    assert str(bare[GRAY_LUT_DESCRIPTOR].VR) == "US"

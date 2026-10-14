"""Every file the DICOM export writes natively is Explicit VR Little Endian (#1107).

Measured on `main` at f31758d3 (1.0.0rc18 + #1105), pydicom 3.0.2:
`export(use_compression=False)`, the #771 fallback for a 32-/64-bit integer
frame, and every file with no pixel element or with floating-point pixels
(under `use_compression=True` too) were written Implicit VR Little Endian,
which states no VR. A reader then took a private element's VR from its
private dictionary. GE `GEMS_PETD_01` `(0009,109D)` is stored `SS` by the
scanner and listed `SL` by pydicom, so `dcmread(...)[0x0009109D]` and an
`iterall()` walk raised `BytesLengthException` ("... (0009,109D) according
to VR 'None'"), and the MIDI-B validation script wrote no results at all.
The JPEG 2000 export wrote the same element `SS 0`: `_merge` already chose
the recorded VR, and only the Implicit VR syntax threw it away.

The owner's rulings (2026-10-14): every native file is Explicit VR LE (Q1);
a value too long for its VR's 2-byte Explicit length is written as the
JPEG 2000 export writes it, `UN` (a private `LO` as `UC`) with its INFO
note (#692, #901; Q2). There is still one rule for choosing a VR.

The source is a Secondary Capture, not a PET image: PET fails
`IODValidator` on its Image Plane module before any of this matters.
Wire VRs are read with `get_item(..., keep_deferred=True)` where pydicom's
`replace_un_with_known_vr` would otherwise relabel them on the way in.
"""
import itertools
import logging
import shutil

import numpy as np
import pydicom
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, ImplicitVRLittleEndian

from isocenter.entities import Instance
from isocenter.io_handlers import ExportContext, _export_instance_worker
from isocenter.session import DicomSession

EXPLICIT = "1.2.840.10008.1.2.1"
IMPLICIT = "1.2.840.10008.1.2"
SC = "1.2.840.10008.5.1.4.1.1.7"
BASIC_TEXT_SR = "1.2.840.10008.5.1.4.1.1.88.11"
GE = "GEMS_PETD_01"
GE_TAG = 0x0009109D            # stored SS by the scanner; pydicom says SL
PIXELS = (np.arange(64, dtype=np.uint16).reshape(8, 8) * 7)

_serial = itertools.count(1)


def _source(folder, *, syntax=ExplicitVRLittleEndian, sop_class=SC,
            pixels=True, extra=None):
    """One file holding `(0009,109D) SS 0` under `GEMS_PETD_01`."""
    folder.mkdir(parents=True, exist_ok=True)
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = sop_class
    meta.MediaStorageSOPInstanceUID = "1.2.826.0.1.1107.1"
    meta.TransferSyntaxUID = syntax
    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID = sop_class
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.PatientID, ds.PatientName = "P1107", "Doe^John"
    ds.StudyInstanceUID = "1.2.826.0.1.1107.2"
    ds.SeriesInstanceUID = "1.2.826.0.1.1107.3"
    ds.Modality = "SR" if sop_class == BASIC_TEXT_SR else "OT"
    ds.StudyDate, ds.StudyTime = "20200101", "120000"
    ds.add_new(0x00090010, "LO", GE)
    ds.add_new(GE_TAG, "SS", 0)
    if pixels:
        ds.Rows = ds.Columns = 8
        ds.BitsAllocated = ds.BitsStored = 16
        ds.HighBit = 15
        ds.PixelRepresentation = 0
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.PixelData = PIXELS.astype("<u2").tobytes()
    if extra is not None:
        extra(ds)
    path = folder / "src.dcm"
    ds.save_as(str(path), enforce_file_format=True,
               implicit_vr=syntax == ImplicitVRLittleEndian,
               little_endian=True)
    return path


def _export(tmp_path, src, *, compress, deidentify=True, reopen=False,
            lock_key=None, name="s"):
    """Ingest `src`'s folder, keep private tags, and export it; the one file."""
    db = str(tmp_path / f"{name}.db")
    out = tmp_path / f"{name}-out"
    with DicomSession(db) as session:
        if lock_key is not None:
            session.enable_reversible_anonymization(lock_key)
        session.ingest(str(src.parent))
        if lock_key is not None:
            session.lock_identities("P1107")
        if deidentify:
            session.configuration.remove_private_tags = False
            session.anonymize(session.audit())
        if reopen:
            session.save(sync=True)
        else:
            session.export(str(out), use_compression=compress,
                           show_progress=False)
    if reopen:
        with DicomSession(db) as session:
            session.export(str(out), use_compression=compress,
                           show_progress=False)
    (written,) = list(out.rglob("*.dcm"))
    return written


def _walks(ds):
    """A validator's walk: every element, nested ones included."""
    for _elem in ds.iterall():
        pass


def _states_ss_zero(written):
    """The issue's failure first: on main `ds[GE_TAG]` raised
    `BytesLengthException`, before any syntax was asked."""
    ds = pydicom.dcmread(str(written))
    elem = ds[GE_TAG]
    assert elem.VR == "SS"
    assert elem.value == 0
    _walks(ds)
    assert str(ds.file_meta.TransferSyntaxUID) == EXPLICIT
    return ds


# ---------------------------------------------------------------------------
# The issue's acceptance case, and its neighbours.
# ---------------------------------------------------------------------------

def test_the_acceptance_case_reads_back_ss_zero_with_the_sources_pixel_bytes(tmp_path):
    """Explicit VR source, private tags kept, `use_compression=False`."""
    src = _source(tmp_path / "src")
    written = _export(tmp_path, src, compress=False)
    ds = _states_ss_zero(written)
    assert bytes(ds.PixelData) == bytes(pydicom.dcmread(str(src)).PixelData)


def test_the_acceptance_case_after_a_reopen(tmp_path):
    """The VR now comes from the store's `value_rep`, not the live graph."""
    src = _source(tmp_path / "src")
    written = _export(tmp_path, src, compress=False, reopen=True)
    ds = _states_ss_zero(written)
    assert bytes(ds.PixelData) == bytes(pydicom.dcmread(str(src)).PixelData)


def test_the_acceptance_case_with_no_de_identification(tmp_path):
    src = _source(tmp_path / "src")
    _states_ss_zero(_export(tmp_path, src, compress=False, deidentify=False))


def test_a_pixel_less_file_on_the_default_path_states_each_vr(tmp_path):
    """A Basic Text SR under `use_compression=True`, the default.

    `_compress_j2k` returns early with no pixels, so the file kept
    whatever `_create_ds` set: on main every SR, KO, PR, RT Plan and
    encapsulated PDF was Implicit VR even under the default.
    """
    src = _source(tmp_path / "src", sop_class=BASIC_TEXT_SR, pixels=False)
    _states_ss_zero(_export(tmp_path, src, compress=True))


def test_re_ingesting_the_uncompressed_export_records_the_private_vr(tmp_path):
    """On main our own re-ingest held `b'\\x00\\x00'` with no VR."""
    src = _source(tmp_path / "src")
    written = _export(tmp_path, src, compress=False)
    again = tmp_path / "again"
    again.mkdir()
    shutil.copy(written, again / "a.dcm")
    with DicomSession(str(tmp_path / "re.db")) as session:
        session.ingest(str(again))
        (patient,) = session.store.patients
        inst = patient.studies[0].series[0].instances[0]
        assert inst.attributes["0009,109d"] == 0
        assert inst.attribute_vrs.get("0009,109d") == "SS"


def test_an_implicit_sources_private_element_of_a_known_creator_is_un_bytes(tmp_path):
    """Unchanged rule (#740): a VR the source never stated is `UN`, with the
    source's own bytes. Explicit VR cannot restore what the source never
    wrote down, and pydicom fails on the source itself the same way.

    Read raw: `ds[tag]` would relabel the `UN` as the dictionary's `SL`.
    """
    src = _source(tmp_path / "src", syntax=ImplicitVRLittleEndian)
    written = _export(tmp_path, src, compress=False)
    ds = pydicom.dcmread(str(written))
    assert str(ds.file_meta.TransferSyntaxUID) == EXPLICIT
    raw = ds.get_item(GE_TAG, keep_deferred=True)
    assert raw.VR == "UN"
    assert bytes(raw.value) == b"\x00\x00"


def test_the_token_payload_syntax_is_not_the_files_syntax(tmp_path):
    """`(0400,0510)` names the token's payload encoding (#790), which stays
    `1.2.840.10008.1.2` though the file around it is now `.1.2.1`: a grep
    for the UID finds `reversibility.PAYLOAD_TRANSFER_SYNTAX`, and it is
    not one of the export's syntax choices."""
    src = _source(tmp_path / "src")
    written = _export(tmp_path, src, compress=False,
                      lock_key=str(tmp_path / "k.key"))
    ds = pydicom.dcmread(str(written))
    assert str(ds.file_meta.TransferSyntaxUID) == EXPLICIT
    item = ds[0x04000500].value[0]
    assert item[0x04000510].VR == "UI"
    assert item[0x04000510].value == IMPLICIT
    assert item[0x04000520].VR == "OB"


# ---------------------------------------------------------------------------
# The worker's other native doors.
# ---------------------------------------------------------------------------

def _image(arr):
    inst = Instance(f"1.2.826.0.1.1107.9.{next(_serial)}", SC, 1)
    inst.file_path = None
    for tag, value in (("0008,0020", "20230101"), ("0008,0030", "120000"),
                       ("0008,0060", "OT"), ("0028,0002", 1)):
        inst.set_attr(tag, value)
    inst.set_pixel_data(arr)
    return inst


def _worker(tmp_path, inst, compression):
    return _export_instance_worker(ExportContext(
        instance=inst,
        output_path=str(tmp_path / "out" / f"{inst.sop_instance_uid}.dcm"),
        patient_attributes={"0010,0010": "ANON", "0010,0020": "PAT1"},
        study_attributes={"0020,000d": "1.2.826.0.2.1"},
        series_attributes={"0020,000e": "1.2.826.0.3.1"},
        compression=compression))


def test_the_771_fallback_is_explicit_vr_and_says_so(tmp_path):
    """A `uint32` frame under `use_compression=True`."""
    arr = (np.arange(16, dtype=np.uint32).reshape(4, 4) * 268435456 + 7)
    outcome = _worker(tmp_path, _image(arr), "j2k")
    assert outcome.ok, outcome.error
    ds = pydicom.dcmread(outcome.output_path)
    assert str(ds.file_meta.TransferSyntaxUID) == EXPLICIT
    assert ds["PixelData"].VR == "OW"
    assert bytes(ds.PixelData) == arr.astype("<u4").tobytes()
    notes = [c for c in outcome.corrections if "written uncompressed" in c]
    assert len(notes) == 1, outcome.corrections
    assert "(Explicit VR Little Endian)" in notes[0], notes[0]


def test_float_pixels_under_the_default_are_explicit_vr(tmp_path):
    arr = np.arange(16, dtype=np.float32).reshape(4, 4) + 0.5
    outcome = _worker(tmp_path, _image(arr), "j2k")
    assert outcome.ok, outcome.error
    ds = pydicom.dcmread(outcome.output_path)
    assert str(ds.file_meta.TransferSyntaxUID) == EXPLICIT
    assert ds[0x7FE00008].VR == "OF"
    assert bytes(ds[0x7FE00008].value) == arr.astype("<f4").tobytes()


ICON = np.arange(16, dtype=np.uint8).reshape(4, 4)


def _icon(ds):
    icon = Dataset()
    icon.Rows = icon.Columns = 4
    icon.BitsAllocated = icon.BitsStored = 8
    icon.HighBit = 7
    icon.PixelRepresentation = 0
    icon.SamplesPerPixel = 1
    icon.PhotometricInterpretation = "MONOCHROME2"
    icon.PixelData = ICON.tobytes()
    ds.IconImageSequence = Sequence([icon])


def test_an_icon_in_an_uncompressed_export_states_its_vr(tmp_path):
    """`_write_back_nested_pixels` gives an 8-bit icon `OB`; under Implicit
    VR no VR reached the file, and now it is on the wire."""
    src = _source(tmp_path / "src", extra=_icon)
    written = _export(tmp_path, src, compress=False, deidentify=False)
    ds = pydicom.dcmread(str(written))
    item = ds.IconImageSequence[0]
    raw = item.get_item(0x7FE00010, keep_deferred=True)
    assert raw.VR == "OB"
    assert bytes(item.PixelData) == ICON.tobytes()
    _walks(ds)


# ---------------------------------------------------------------------------
# Q2: a value too long for its VR's 2-byte Explicit length.
# ---------------------------------------------------------------------------

def _un_notes(caplog):
    return [r.getMessage() for r in caplog.records
            if "is written as UN" in r.getMessage()]


def _uc_notes(caplog):
    return [r.getMessage() for r in caplog.records
            if "is written as UC" in r.getMessage()]


def _set_and_export(tmp_path, mutate, caplog):
    src = _source(tmp_path / "src")
    out = tmp_path / "out"
    with caplog.at_level(logging.INFO, logger="isocenter"), \
            DicomSession(str(tmp_path / "a.db")) as session:
        session.ingest(str(src.parent))
        (patient,) = session.store.patients
        mutate(patient.studies[0].series[0].instances[0])
        session.export(str(out), use_compression=False, show_progress=False)
    (written,) = list(out.rglob("*.dcm"))
    return written


def _re_ingested(tmp_path, written):
    again = tmp_path / "again"
    again.mkdir()
    shutil.copy(written, again / "a.dcm")
    with DicomSession(str(tmp_path / "b.db")) as session:
        session.ingest(str(again))
        (patient,) = session.store.patients
        attrs = dict(patient.studies[0].series[0].instances[0].attributes)
        losses = session.store_backend.get_audit_losses()
    return attrs, losses


def test_a_standard_value_over_the_short_length_is_un_with_its_note(tmp_path, caplog):
    """A caller's 40000-value `US` list: written whole under Implicit VR on
    main, with no note; now `UN` and said, as the JPEG 2000 export does."""
    values = list(range(40000))
    written = _set_and_export(
        tmp_path, lambda i: i.set_attr("0018,1310", values), caplog)
    ds = pydicom.dcmread(str(written))
    assert str(ds.file_meta.TransferSyntaxUID) == EXPLICIT
    assert ds.get_item(0x00181310, keep_deferred=True).VR == "UN"
    notes = _un_notes(caplog)
    assert len(notes) == 1 and "0018,1310" in notes[0], notes
    attrs, losses = _re_ingested(tmp_path, written)
    assert not [r for r in losses if "0018,1310" in r[2]], losses
    assert list(attrs["0018,1310"]) == values


def test_a_private_lo_over_the_short_length_is_uc_with_its_note(tmp_path, caplog):
    def mutate(inst):
        inst.set_attr("0011,0010", "ACME")
        inst.set_attr("0011,1001", list(range(40000)))

    written = _set_and_export(tmp_path, mutate, caplog)
    elem = pydicom.dcmread(str(written))[0x00111001]
    assert elem.VR == "UC"
    assert elem.VM == 40000
    notes = _uc_notes(caplog)
    assert len(notes) == 1 and "0011,1001" in notes[0], notes
    attrs, losses = _re_ingested(tmp_path, written)
    assert not [r for r in losses if "0011,1001" in r[2]], losses
    assert [str(v) for v in attrs["0011,1001"]] == [str(i) for i in range(40000)]

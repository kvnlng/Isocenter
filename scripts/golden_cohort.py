"""Build the synthetic members of the output fingerprint's cohort (#717).

    python -m scripts.golden_cohort [--out fingerprint/cohort] [--only NAME[,NAME]]

Each member is one directory under `fingerprint/cohort/`, holding the
DICOM files one session ingests. The **committed bytes are the authority**,
not this generator: a change here, or in pydicom's writer, must not
silently change an input, so this script never overwrites a member that
already exists. It writes the members that are missing. To add a member:
add a builder to `MEMBERS`, run this, commit the new directory, and retake
`fingerprint/output.json` in the same change (`scripts/output_fingerprint.py`).
A member is never removed or rebuilt to make a difference go away.

Every UID is fixed, under `2.25.` (derived from a UUID5 of the member and
role, so it is stable and globally unique), never `generate_uid()`, whose
random UIDs would make the input differ on every run; and every file meta
carries this cohort's own implementation identity rather than pydicom's,
so no committed input names the pydicom version that wrote it. File names
start with the member's name so that no basename collides with a test's
text (RELEASING.md selects tests by basename).

Each member exists to make one behaviour visible in the fingerprint;
`MEMBERS` says which. A member marked varying gets a `VARIES` file holding
the reason; see `scripts/output_fingerprint.py` for what that does and how
the mark retires itself.
"""
from __future__ import annotations

import argparse
import struct
import sys
import uuid
from pathlib import Path

import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import (ExplicitVRBigEndian, ExplicitVRLittleEndian,
                         ImplicitVRLittleEndian)

REPO = Path(__file__).resolve().parents[1]
COHORT = REPO / "fingerprint" / "cohort"

CT = "1.2.840.10008.5.1.4.1.1.2"
PARAMETRIC_MAP = "1.2.840.10008.5.1.4.1.1.30"
ENCAPSULATED_PDF = "1.2.840.10008.5.1.4.1.1.104.1"
NAMESPACE = uuid.UUID("6f1c2b1e-7170-4a17-9f0e-000000000717")
IMPLEMENTATION_VERSION = "ISOCENTER_GOLD"


def uid(*parts) -> str:
    """A fixed UID for `parts`: 2.25.<UUID5 as an integer>."""
    return "2.25." + str(uuid.uuid5(NAMESPACE, "/".join(str(p) for p in parts)).int)


IMPLEMENTATION_UID = uid("implementation")


def _meta(sop_class, sop_instance, syntax) -> FileMetaDataset:
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = sop_class
    meta.MediaStorageSOPInstanceUID = sop_instance
    meta.TransferSyntaxUID = syntax
    meta.ImplementationClassUID = IMPLEMENTATION_UID
    meta.ImplementationVersionName = IMPLEMENTATION_VERSION
    return meta


def ct(member, study=1, series=1, inst=1, pid=None, serial="GOLD-SN-1",
       rows=16, cols=16, syntax=ExplicitVRLittleEndian, study_date="20230101"):
    """A small CT with patient, study and equipment identifiers to remediate."""
    sop = uid(member, study, series, inst)
    ds = FileDataset(None, {}, file_meta=_meta(CT, sop, syntax), preamble=b"\0" * 128)
    ds.SpecificCharacterSet = "ISO_IR 100"
    ds.PatientID = pid or f"GOLD-{member}"
    ds.PatientName, ds.PatientBirthDate, ds.PatientSex = "GOLDEN^PATIENT", "19600101", "F"
    ds.PatientAge = "063Y"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = uid(member, study), uid(member, study, series)
    ds.SOPInstanceUID, ds.SOPClassUID = sop, CT
    ds.FrameOfReferenceUID = uid(member, study, "frame")
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "CT", series, inst
    ds.StudyID = f"S{study}"
    if study_date is not None:
        ds.StudyDate = study_date
        ds.SeriesDate = ds.AcquisitionDate = ds.ContentDate = study_date
    ds.StudyTime = ds.SeriesTime = "120000"
    ds.AccessionNumber, ds.InstitutionName = f"ACC{study}", "General Hospital"
    ds.ReferringPhysicianName = "REF^DOC"
    ds.StudyDescription, ds.SeriesDescription = "Chest", "Axial"
    ds.Manufacturer, ds.ManufacturerModelName = "Acme", "Golden"
    ds.DeviceSerialNumber = serial
    # Non-canonical DS spellings on purpose: a saved-and-reopened export
    # re-spells them today (#662), which only a spelling like this shows.
    ds.SliceThickness, ds.KVP = "1.000000", "120"
    ds.ImagePositionPatient = ["0", "0", f"{inst}.000000"]
    ds.ImageOrientationPatient = ["1", "0", "0", "0", "1", "0"]
    ds.PixelSpacing = ["0.500000", "0.5"]
    ds.Rows, ds.Columns, ds.SamplesPerPixel = rows, cols, 1
    ds.PhotometricInterpretation, ds.PixelRepresentation = "MONOCHROME2", 0
    ds.BitsAllocated, ds.BitsStored, ds.HighBit = 16, 16, 15
    arr = (np.arange(rows * cols, dtype=np.uint32).reshape(rows, cols) * 37 + inst) % 65536
    ds.PixelData = arr.astype("<u2").tobytes()
    return ds


def write(ds, path: Path, syntax=None):
    syntax = syntax or ds.file_meta.TransferSyntaxUID
    path.parent.mkdir(parents=True, exist_ok=True)
    pydicom.dcmwrite(str(path), ds, implicit_vr=syntax == ImplicitVRLittleEndian,
                     little_endian=syntax != ExplicitVRBigEndian,
                     force_encoding=True)


# -- members ---------------------------------------------------------------

def longitudinal(out: Path):
    """One patient, two studies of three instances: pseudonym, per-patient
    date shift and its interval, folder naming, and the reopened arm's
    re-spelling of DS/IS values (#662)."""
    for study in (1, 2):
        for inst in (1, 2, 3):
            write(ct("longitudinal", study=study, inst=inst, pid="GOLD-LONG",
                     study_date=f"2023010{study}"),
                  out / f"longitudinal-s{study}-{inst}.dcm")


def private_nested(out: Path):
    """A private block (LO, OW, multi-valued DS, a private SQ holding a PN)
    and PHI inside ReferencedImageSequence: nested remediation (#57), and
    private VR carriage in B.dicom-j2k (#676)."""
    ds = ct("private_nested")
    block = ds.private_block(0x0029, "GOLDEN PRIVATE", create=True)
    block.add_new(0x01, "LO", "private text")
    block.add_new(0x02, "OW", np.arange(4, dtype="<u2").tobytes())
    block.add_new(0x03, "DS", ["1.500000", "2.0"])
    inner = Dataset()
    inner.PatientName, inner.CodeValue = "NESTED^NAME", "X"
    block.add_new(0x04, "SQ", Sequence([inner]))
    ref = Dataset()
    ref.ReferencedSOPClassUID = CT
    ref.ReferencedSOPInstanceUID = uid("longitudinal", 1, 1, 1)
    ref.PatientName = "NESTED^PHI"
    ds.ReferencedImageSequence = Sequence([ref])
    write(ds, out / "private_nested-1.dcm")


def redacted(out: Path):
    """A CT whose serial the configuration's machine rule matches: the
    redacted pixels, and the SOP Instance UID the redaction derives under
    the cohort's secret. Marked varying until #544, when that UID was
    random."""
    write(ct("redacted", serial="GOLD-SN-REDACT", rows=32, cols=32),
          out / "redacted-1.dcm")


def curve_overlay(out: Path):
    """A 5000,xxxx curve and a full 6000,xxxx overlay module (#556)."""
    ds = ct("curve_overlay")
    ds.add_new(0x50000005, "US", 2)
    ds.add_new(0x50000010, "US", 2)
    ds.add_new(0x50000020, "CS", "TAC")
    ds.add_new(0x50000103, "US", 0)
    ds.add_new(0x50003000, "OW", np.array([1, 2, 3, 4], dtype="<u2").tobytes())
    ds.add_new(0x60000010, "US", 16)
    ds.add_new(0x60000011, "US", 16)
    ds.add_new(0x60000040, "CS", "G")
    ds.add_new(0x60000050, "SS", [1, 1])
    ds.add_new(0x60000100, "US", 1)
    ds.add_new(0x60000102, "US", 0)
    ds.add_new(0x60003000, "OW", bytes(range(32)))
    write(ds, out / "curve_overlay-1.dcm")


def implicit(out: Path):
    """An Implicit VR Little Endian source: the implicit read path."""
    write(ct("implicit", syntax=ImplicitVRLittleEndian), out / "implicit-1.dcm")


def ecg(out: Path):
    """A 12-lead ECG with a SEGMENT and a POINT annotation: WFDB .hea/.dat,
    and the Murmur annotations bridge."""
    from scripts.generate_waveform_test_data import add_annotation, build_ecg_dataset
    ds = build_ecg_dataset(num_samples=1000, patient_id="GOLD-ecg")
    sop = uid("ecg", 1, 1, 1)
    ds.file_meta = _meta(ds.SOPClassUID, sop, ExplicitVRLittleEndian)
    ds.SOPInstanceUID = sop
    ds.StudyInstanceUID, ds.SeriesInstanceUID = uid("ecg", 1), uid("ecg", 1, 1)
    add_annotation(ds, 100, 200, text="AF episode")
    add_annotation(ds, 300)
    fds = FileDataset(None, ds, file_meta=ds.file_meta, preamble=b"\0" * 128)
    write(fds, out / "ecg-1.dcm", ExplicitVRLittleEndian)


def lut_ambiguous(out: Path):
    """A Modality LUT Sequence whose descriptor and data have ambiguous VRs
    (US/SS, US/OW), in an implicit source: J12, #691. None of the 250
    pydicom files reached it."""
    ds = ct("lut_ambiguous", syntax=ImplicitVRLittleEndian)
    item = Dataset()
    item.add_new(0x00283002, "US", [4, 0, 16])
    item.add_new(0x00283003, "LO", "GOLD LUT")
    item.add_new(0x00283004, "LO", "HU")
    item.add_new(0x00283006, "OW", np.array([0, 1000, 40000, 65535], dtype="<u2").tobytes())
    ds.ModalityLUTSequence = Sequence([item])
    write(ds, out / "lut_ambiguous-1.dcm")


def big_lut(out: Path):
    """A VOI LUT of 65536 16-bit entries (descriptor first value 0), 131072
    bytes of LUT Data, in an explicit source: #902. The largest table a
    descriptor can declare; above 65534 bytes it was dropped. No other
    member reaches the non-segmented LUT Data arm (ALOKA's are segmented
    palettes)."""
    ds = ct("big_lut")
    item = Dataset()
    item.add_new(0x00283002, "US", [0, 0, 16])
    item.add_new(0x00283003, "LO", "GOLD VOI LUT")
    item.add_new(0x00283006, "OW", (np.arange(65536, dtype=np.uint32) * 7 % 65536)
                 .astype("<u2").tobytes())
    ds.VOILUTSequence = Sequence([item])
    write(ds, out / "big_lut-1.dcm")


def big_endian_words(out: Path):
    """Explicit VR Big Endian with an OW palette table and a private OW:
    the J7 byte order and VR arms."""
    ds = ct("big_endian_words", syntax=ExplicitVRBigEndian)
    ds.add_new(0x00281201, "OW", np.array([0, 1000, 40000, 65535], dtype=">u2").tobytes())
    ds.add_new(0x00090010, "LO", "GOLDEN BE")
    ds.add_new(0x00091010, "OW", np.array([0, 1000, 40000, 65535], dtype=">u2").tobytes())
    ds.PixelData = np.frombuffer(ds.PixelData, "<u2").astype(">u2").tobytes()
    write(ds, out / "big_endian_words-1.dcm")


def float_pixels(out: Path):
    """A Parametric Map with Float Pixel Data: the float export path."""
    sop = uid("float_pixels", 1, 1, 1)
    ds = FileDataset(None, {}, file_meta=_meta(PARAMETRIC_MAP, sop, ExplicitVRLittleEndian),
                     preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "GOLD-float", "GOLDEN^FLOAT"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = uid("float_pixels", 1), uid("float_pixels", 1, 1)
    ds.SOPInstanceUID, ds.SOPClassUID = sop, PARAMETRIC_MAP
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "OT", 1, 1
    ds.StudyDate, ds.StudyTime = "20230101", "120000"
    ds.Rows = ds.Columns = 4
    ds.BitsAllocated = ds.BitsStored = 32
    ds.HighBit = 31
    ds.SamplesPerPixel, ds.PixelRepresentation = 1, 0
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.add_new(0x7FE00008, "OF", (np.arange(16, dtype="<f4") + 0.5).tobytes())
    write(ds, out / "float_pixels-1.dcm")


def no_study_date(out: Path):
    """A study with no StudyDate: the NoDate folder and the date rows."""
    write(ct("no_study_date", study_date=None), out / "no_study_date-1.dcm")


def no_patient_id(out: Path):
    """Two subjects with no Patient ID in one session (#584): `ALPHA`, the
    element absent, over two studies, and `BETA`, the element empty, over
    one. Each study is its own patient with its own date offset, and every
    file exports an empty Patient ID. Nothing in pydicom's corpus has two
    ID-less subjects in one session."""
    for study, name in ((1, "ALPHA^ONE"), (2, "ALPHA^ONE"), (3, "BETA^TWO")):
        ds = ct("no_patient_id", study=study, study_date=f"2023020{study}")
        ds.PatientName = name
        if study == 3:
            ds.PatientID = ""
        else:
            del ds.PatientID
        write(ds, out / f"no_patient_id-s{study}-1.dcm")


def withheld(out: Path):
    """A CT carrying a Study Date inside `ReferencedImageSequence`, spelled
    `2023.01.01`, which no date shift can read: the nested shift declines
    on the format, the item keeps the date, and `export(check_burned_in=
    True)` withholds the instance. The withholding path on the fingerprint:
    #624 wrote the corpus's three withheld instances, whose only unreadable
    date was the top-level Study Date the export stamps from the Study. A
    nested copy is not stamped (#496 N4), so a change to what the export
    stamps does not move this member."""
    ds = ct("withheld")
    item = Dataset()
    item.ReferencedSOPClassUID = CT
    item.ReferencedSOPInstanceUID = uid("withheld", "referenced")
    item.StudyDate = "2023.01.01"
    ds.ReferencedImageSequence = Sequence([item])
    write(ds, out / "withheld-1.dcm")


def prior_markers(out: Path):
    """A CT another tool already de-identified (#554): Patient Identity
    Removed `YES`, two De-identification Method values, a `113100`/`DCM`
    code item and Longitudinal Temporal Information Modified `MODIFIED`.
    No other member carries markers, so without this one the merge -- ours
    appended after theirs, their code item passed through, `(0028,0303)`
    replaced or kept -- would be invisible."""
    ds = ct("prior_markers")
    ds.PatientIdentityRemoved = "YES"
    ds.DeidentificationMethod = ["OtherTool 3.2", "site profile 7"]
    item = Dataset()
    item.CodeValue, item.CodingSchemeDesignator = "113100", "DCM"
    item.CodeMeaning = "Basic Application Confidentiality Profile"
    ds.DeidentificationMethodCodeSequence = Sequence([item])
    ds.LongitudinalTemporalInformationModified = "MODIFIED"
    write(ds, out / "prior_markers-1.dcm")


def graphic_annotation(out: Path):
    """A CT with a Graphic Annotation Sequence whose objects hold free text
    no row of the table reaches -- a text object's Tracking ID and a
    ruler's Tick Label -- beside an Unformatted Text Value and a Tracking
    UID, which are rows. `basic@2026c` empties the sequence (#840). No
    other member carries one."""
    ds = ct("graphic_annotation")
    text = Dataset()
    text.AnchorPointAnnotationUnits = "PIXEL"
    text.AnchorPoint = [4.0, 4.0]
    text.AnchorPointVisibility = "Y"
    text.UnformattedTextValue = "GOLD annotation text"
    text.TrackingID = "GOLD tracking label"
    text.TrackingUID = uid("graphic_annotation", "tracking")
    tick = Dataset()
    tick.TickPosition = 0.5
    tick.TickLabel = "GOLD tick"
    ruler = Dataset()
    ruler.CompoundGraphicInstanceID = 1
    ruler.CompoundGraphicUnits = "PIXEL"
    ruler.GraphicDimensions = 2
    ruler.NumberOfGraphicPoints = 2
    ruler.GraphicData = [0.0, 0.0, 8.0, 8.0]
    ruler.CompoundGraphicType = "RULER"
    ruler.MajorTicksSequence = Sequence([tick])
    ruler.TickAlignment = "CENTER"
    ruler.TickLabelAlignment = "TOP"
    ruler.ShowTickLabel = "Y"
    annotation = Dataset()
    annotation.GraphicLayer = "GOLD"
    annotation.TextObjectSequence = Sequence([text])
    annotation.CompoundGraphicSequence = Sequence([ruler])
    ds.GraphicAnnotationSequence = Sequence([annotation])
    write(ds, out / "graphic_annotation-1.dcm")


def multi_valued_keys(out: Path):
    """Four CTs, each with one linkage key holding two values: Patient ID,
    SOP Instance UID, Study Instance UID, Series Instance UID (#747). Every
    file is refused at ingest with an `ERROR` row naming the element and
    the count, so this member exports nothing and its rows are the whole
    recording. No other member holds a multi-valued key; and the row this
    one replaced quoted a `TypeError` whose words differ between 3.12 and
    3.14t."""
    for n, (keyword, label) in enumerate((
            ("PatientID", "patient-id"), ("SOPInstanceUID", "sop-uid"),
            ("StudyInstanceUID", "study-uid"),
            ("SeriesInstanceUID", "series-uid")), start=1):
        ds = ct("multi_valued_keys", study=n)
        first = getattr(ds, keyword)
        second = ("GOLD-multi_valued_keys-B" if keyword == "PatientID"
                  else uid("multi_valued_keys", "second", keyword))
        setattr(ds, keyword, [first, second])
        write(ds, out / f"multi_valued_keys-{label}.dcm")


def lut_unusable_descriptor(out: Path):
    """Three CTs whose Modality LUT has a LUT Descriptor pydicom cannot
    read a first value from (#703): empty in an Implicit VR and in an
    Explicit VR source, and holding one value in an Explicit VR source.
    The implicit file was refused at ingest and the explicit ones lost
    their LUT Data at export; all three now export it as `OW` with one
    `WARNING` clause. `lut_ambiguous` carries a three-value descriptor and
    never reached either door's fallback."""
    for inst, (label, descriptor, syntax) in enumerate((
            ("empty-implicit", None, ImplicitVRLittleEndian),
            ("empty-explicit", None, ExplicitVRLittleEndian),
            ("one-value-explicit", [4], ExplicitVRLittleEndian)), start=1):
        ds = ct("lut_unusable_descriptor", inst=inst, syntax=syntax)
        item = Dataset()
        item.add_new(0x00283002, "US", descriptor)
        item.add_new(0x00283003, "LO", "GOLD LUT")
        item.add_new(0x00283004, "LO", "HU")
        item.add_new(0x00283006, "OW", np.array([0, 1000, 40000, 65535], dtype="<u2").tobytes())
        ds.ModalityLUTSequence = Sequence([item])
        write(ds, out / f"lut_unusable_descriptor-{label}.dcm")


def encapsulated_pdf(out: Path):
    """An Encapsulated PDF: a 1000-byte document with Encapsulated Document
    Length 1000, and no pixels (#757). `basic@2026c` writes its two-byte
    dummy over the document, and the length written follows it. No other
    member carries (0042,0011) or (0042,0015)."""
    sop = uid("encapsulated_pdf", 1, 1, 1)
    ds = FileDataset(None, {}, file_meta=_meta(ENCAPSULATED_PDF, sop, ExplicitVRLittleEndian),
                     preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "GOLD-pdf", "GOLDEN^PDF"
    ds.StudyInstanceUID = uid("encapsulated_pdf", 1)
    ds.SeriesInstanceUID = uid("encapsulated_pdf", 1, 1)
    ds.SOPInstanceUID, ds.SOPClassUID = sop, ENCAPSULATED_PDF
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "DOC", 1, 1
    ds.StudyDate, ds.StudyTime = "20230101", "120000"
    ds.ContentDate, ds.ContentTime = "20230101", "120000"
    ds.ConversionType = "WSD"
    ds.BurnedInAnnotation = "NO"
    ds.DocumentTitle = "GOLD report"
    ds.MIMETypeOfEncapsulatedDocument = "application/pdf"
    head = b"%PDF-1.4\n% GOLDEN^PDF 1960-01-01\n"
    ds.EncapsulatedDocument = head + b"x" * (1000 - len(head))
    ds.EncapsulatedDocumentLength = 1000
    write(ds, out / "encapsulated_pdf-1.dcm")


def unstated_private_vr(out: Path):
    """Private elements of creators pydicom's private dictionary knows,
    whose VR the file does not state (#740): `-implicit` under Implicit
    VR, `-explicit-un` with `UN` on the wire, and `-mismatch`, six text
    bytes where the dictionary says `SL`, which refused the whole file.
    A Siemens MR header's CS, LO, US and FD, a Siemens CSA header, and a
    private date, so the `(0028,0303)` effect is on the fingerprint too.
    Each is held as the file's bytes and written `UN` in B.dicom-j2k. A
    scan of every other input found no such element."""
    block = (  # element, the dictionary's VR, the value, its bytes
        (0x00191008, "CS", "IMAGE NUM 4", b"IMAGE NUM 4 "),
        (0x00191009, "LO", "1.0", b"1.0 "),
        (0x0019100A, "US", 16, struct.pack("<H", 16)),
        (0x0019100E, "FD", [0.0, 0.5, -1.0], struct.pack("<3d", 0.0, 0.5, -1.0)),
        (0x00291010, "OB", b"SV10\x04\x03\x02\x01", b"SV10\x04\x03\x02\x01"),
        (0x3109100A, "DA", "20230115", b"20230115"))

    def creators(ds):
        ds.add_new(0x00190010, "LO", "SIEMENS MR HEADER")
        ds.add_new(0x00290010, "LO", "SIEMENS CSA HEADER")
        ds.add_new(0x31090010, "LO", "Applicare/RadWorks/Version 5.0")

    # Implicit VR writes no VR, so the dictionary's own VRs are the way to
    # put these bytes on the wire with none.
    ds = ct("unstated_private_vr", inst=1, syntax=ImplicitVRLittleEndian)
    creators(ds)
    for tag, vr, value, _ in block:
        ds.add_new(tag, vr, value)
    write(ds, out / "unstated_private_vr-implicit.dcm")

    ds = ct("unstated_private_vr", inst=2)
    creators(ds)
    for tag, _, _, raw in block:
        ds.add_new(tag, "UN", raw)
    write(ds, out / "unstated_private_vr-explicit-un.dcm")

    ds = ct("unstated_private_vr", inst=3)
    ds.add_new(0x00190010, "LO", "SIEMENS MR HEADER")
    ds.add_new(0x00191012, "UN", b"12.5 \x00")
    write(ds, out / "unstated_private_vr-mismatch.dcm")


def ecg_lead_codes(out: Path):
    """Five ECGs of five patients whose Channel Source codes the WFDB lead
    names turn on (#832). `-2008-12lead`: twelve leads coded to PS3.16's
    2008 lead table, where `MDC 2:3` is both Lead III and Lead V1, with a
    mark on each (channels 3 and 7); the Code Meaning tells them apart.
    `-2008-limb`: leads I, II and III of that table, a `2:3` with no V1
    beside it. `-padded`: Code Values with a space in front, behind and on
    both sides. `-coded-twice`: Lead II on two channels, so both are
    written as `2:2`, with the `WARNING` row and a mark on channel 2.
    `-short`: three Channel Definitions over two sample columns, with a
    mark on channel 2 and one on channel 3, which the record has no signal
    for and which therefore carries no `lead` (#963). The
    `ecg` member's codes are reference IDs, which no table names, and
    pydicom's own ECG is SCP-ECG coded: neither holds a `2:3`, a padded
    code or a shared name."""
    import warnings

    from scripts.generate_waveform_test_data import add_annotation, build_ecg_dataset
    files = (
        ("2008-12lead", (3, 7), (
            ("2:1", "Lead I", "I"), ("2:2", "Lead II", "II"),
            ("2:3", "Lead III", "III"),
            ("2:62", "aVR, augmented voltage, right", "aVR"),
            ("2:63", "aVL, augmented voltage, left", "aVL"),
            ("2:64", "aVF, augmented voltage, foot", "aVF"),
            ("2:3", "Lead V1", "V1"), ("2:4", "Lead V2", "V2"),
            ("2:5", "Lead V3", "V3"), ("2:6", "Lead V4", "V4"),
            ("2:7", "Lead V5", "V5"), ("2:8", "Lead V6", "V6"))),
        ("2008-limb", (), (
            ("2:1", "Lead I", "I"), ("2:2", "Lead II", "II"),
            ("2:3", "Lead III", "III"))),
        ("padded", (), (
            (" 2:1", "Lead I", "I"), ("2:2 ", "Lead II", "II"),
            (" 2:61 ", "Lead III", "III"),
            ("2:62", "aVR, augmented voltage, right", "aVR"))),
        ("coded-twice", (2,), (
            ("2:2", "Lead II", "II"), ("2:2", "Lead II", "II"))),
        ("short", (2, 3), (
            ("2:1", "Lead I", "I"), ("2:2", "Lead II", "II"),
            ("2:61", "Lead III", "III"))))
    # `-short` alone holds fewer sample columns than Channel Definitions.
    columns = {"short": 2}
    for n, (label, marked, channels) in enumerate(files, start=1):
        # The committed bytes are the authority for good, so the member is
        # conformant apart from what it is there to show. The fixture
        # writes one string as both Code Meaning and Channel Label, and
        # the table's own meanings (29 characters) overflow the label's
        # SH 16: it is handed the short name, and the meaning is set
        # after. Built with warnings as errors, so pydicom's length check
        # would stop the build; only the fixture's own pydicom 4
        # deprecation notice is let through.
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            warnings.simplefilter("ignore", DeprecationWarning)
            ds = build_ecg_dataset(
                num_samples=1000, patient_id=f"GOLD-ecg_lead_codes-{n}",
                channels=[(code, name) for code, _meaning, name in channels])
            sop = uid("ecg_lead_codes", n, 1, 1)
            ds.file_meta = _meta(ds.SOPClassUID, sop, ExplicitVRLittleEndian)
            ds.SOPInstanceUID = sop
            ds.StudyInstanceUID = uid("ecg_lead_codes", n)
            ds.SeriesInstanceUID = uid("ecg_lead_codes", n, 1)
            definitions = ds.WaveformSequence[0].ChannelDefinitionSequence
            for chdef, (_code, meaning, _name) in zip(definitions, channels):
                chdef.ChannelSourceSequence[0].CodeMeaning = meaning
            for channel in marked:
                add_annotation(ds, 100 * channel, channel=channel)
            if label in columns:
                group = ds.WaveformSequence[0]
                samples = np.frombuffer(group.WaveformData, dtype="<i2")
                group.NumberOfWaveformChannels = columns[label]
                group.WaveformData = samples.reshape(
                    -1, len(channels))[:, :columns[label]].tobytes()
            fds = FileDataset(None, ds, file_meta=ds.file_meta, preamble=b"\0" * 128)
            write(fds, out / f"ecg_lead_codes-{label}.dcm", ExplicitVRLittleEndian)


def multi_valued_pn(out: Path):
    """Person Names holding several values (#937). Each was ingested, and
    where kept exported, as one value holding the text of a Python list
    (`[A^B, C^D]`). Patient's Name (VM 1) and Operators' Name (VM 1-n),
    which `basic` replaces whole; Evaluator Name `(0014,2006)`, a PN no
    row of Table E.1-1 names, at the top level and inside Referenced
    Image Sequence, so a kept one is on the fingerprint under both
    configurations; and a private PN of two values beside one of one
    value, stated `PN` by this Explicit VR file, for B.dicom-j2k's wire
    VR. No other input holds a PN of more than one value."""
    def pn(dataset, tag, text):
        # Past pydicom's VM check: a two-valued Patient's Name is the point.
        dataset[tag] = pydicom.DataElement(
            tag, "PN", text, validation_mode=pydicom.config.IGNORE)

    ds = ct("multi_valued_pn")
    pn(ds, 0x00100010, "GOLDEN^ONE\\GOLDEN^TWO")
    pn(ds, 0x00081070, "OPERATOR^ONE\\OPERATOR^TWO")
    pn(ds, 0x00142006, "EVALUATOR^ONE\\EVALUATOR^TWO")
    ref = Dataset()
    ref.ReferencedSOPClassUID = CT
    ref.ReferencedSOPInstanceUID = uid("longitudinal", 1, 1, 1)
    pn(ref, 0x00142006, "NESTED^ONE\\NESTED^TWO")
    ds.ReferencedImageSequence = Sequence([ref])
    ds.add_new(0x00710010, "LO", "GOLDEN PN")
    pn(ds, 0x00711001, "PRIVATE^ONE\\PRIVATE^TWO")
    pn(ds, 0x00711002, "PRIVATE^ONLY")
    write(ds, out / "multi_valued_pn-1.dcm")


def multi_valued_series_fields(out: Path):
    """Five CTs of one patient, one study each, each with two values in
    one element ingest builds the `series` row from (#985): Manufacturer,
    Manufacturer's Model Name, Device Serial Number, Modality and Series
    Number. Any one of them made `ingest()` raise
    `sqlite3.ProgrammingError`, so the member's session stored and
    exported nothing; each is now ingested with the field held as the
    source's text (0 for Series Number) and its own element exported as
    the source wrote it, where the policy keeps it. No other input holds
    two values in any of the five."""
    def two(dataset, keyword, text):
        # Past pydicom's VM check: each of the five takes one value.
        tag = pydicom.datadict.tag_for_keyword(keyword)
        dataset[tag] = pydicom.DataElement(
            tag, pydicom.datadict.dictionary_VR(tag), text,
            validation_mode=pydicom.config.IGNORE)

    for n, (keyword, label, text) in enumerate((
            ("Manufacturer", "manufacturer", "Acme\\Imaging"),
            ("ManufacturerModelName", "model", "Golden\\Mark2"),
            ("DeviceSerialNumber", "serial", "GOLD-SN-1\\GOLD-SN-2"),
            ("Modality", "modality", "CT\\MR"),
            ("SeriesNumber", "series-number", "1\\2")), start=1):
        ds = ct("multi_valued_series_fields", study=n)
        two(ds, keyword, text)
        write(ds, out / f"multi_valued_series_fields-{label}.dcm")


def number_vr_on_a_date_or_time(out: Path):
    """A date, a time and a date-time the file wrote under a numeric string
    VR: Date of Gain Calibration `(0014,3076)` under `IS`, Time of Gain
    Calibration `(0014,3077)` under `DS`, Expiration DateTime `(0008,0416)`
    under `DS`. No row of Table E.1-1 names any of the three, so each is
    kept under both configurations. pydicom reads them as `IS` and
    `DSfloat`, numbers that carry the file's own text, and the exporter
    writes them under the dictionary's DA, TM and DT from that text
    (review of #1009: a gate on "a number under a text VR" lost all
    three). No other input states a VR that disagrees with the
    dictionary for a standard tag."""
    ds = ct("number_vr_on_a_date_or_time")
    for tag, vr, text in ((0x00143076, "IS", "20230101"),
                          (0x00143077, "DS", "120000.5"),
                          (0x00080416, "DS", "20230101120000.5")):
        ds[tag] = pydicom.DataElement(tag, vr, text)
    write(ds, out / "number_vr_on_a_date_or_time-1.dcm")


def binary_number_vr_on_a_text_tag(out: Path):
    """Three text tags the file wrote under a binary numeric VR:
    Manufacturer's Model Name `(0008,1090)`, an LO, under `US`; Patient
    Position `(0018,5100)`, a CS, under `SS`; Manufacturer `(0008,0070)`,
    an LO, under `FD`. No row of Table E.1-1 names any of the three.
    pydicom reads each as a plain `int` or `float`, which nothing in the
    graph tells from a caller's number, so the exporter's text gate drops
    each under a `DATA_LOSS` row and writes the rest of the file (#939;
    owner ruling of 2026-10-09 on the delta review of #1009; #1017).
    Before the gate the whole file failed. The sibling of
    `number_vr_on_a_date_or_time`, whose `IS` and `DS` values are
    pydicom's own classes and are written."""
    ds = ct("binary_number_vr_on_a_text_tag")
    for tag, vr, value in ((0x00081090, "US", 42), (0x00185100, "SS", -3),
                           (0x00080070, "FD", 4.5)):
        ds[tag] = pydicom.DataElement(tag, vr, value)
    write(ds, out / "binary_number_vr_on_a_text_tag-1.dcm")


def source_says_unmodified(out: Path):
    """A CT whose source states Longitudinal Temporal Information Modified
    `(0028,0303) UNMODIFIED`, with no Study Date (so neither configuration
    shifts anything), a Series Date and a Content Date `basic` writes the
    dummy to and a Birth Date it empties (this store's record), and a
    Study Update DateTime `(0008,041f)` no row of Table E.1-1 names, kept
    as ingested. Nothing shifted and a date as found: the export stamps no
    `(0028,0303)` of its own, and the source's `UNMODIFIED` is not carried
    beside the dates this store took (#1011). No other member states
    `UNMODIFIED`, and every other member with a date as found also has a
    shifted Study Date, which writes `MODIFIED`."""
    ds = ct("source_says_unmodified", study_date=None)
    ds.SeriesDate = ds.ContentDate = "20230101"
    ds.add_new(0x0008041F, "DT", "20230101120000")
    ds.LongitudinalTemporalInformationModified = "UNMODIFIED"
    write(ds, out / "source_says_unmodified-1.dcm")


def linkage_key_not_text(out: Path):
    """An ordinary CT and five CTs of five other patients, each stating a
    VR that is not text for one linkage key (#1022): SOP Instance UID
    under `OB`, Patient ID under `US`, Study Instance UID under `OB`,
    Series Instance UID under `IS`, Patient ID under `SQ`. The first two
    made `ingest()` raise `ValueError` and store nothing; the next two
    were ingested, linked under the Python text of the value
    (`b'…'`, `7`); the last was refused with a row quoting a `TypeError`
    whose words differ between 3.12 and 3.14t. Each of the five is now
    refused at ingest with an `ERROR` row naming the element and the VR,
    and the ordinary file is ingested and exported. No other input states
    a VR that is not text for a linkage key."""
    write(ct("linkage_key_not_text", study=1, pid="GOLD-linkage_key_not_text-good"),
          out / "linkage_key_not_text-good.dcm")
    item = Dataset()
    item.CodeValue = "GOLD"
    for n, (tag, vr, value, label) in enumerate((
            (0x00080018, "OB", None, "sop-uid-ob"),
            (0x00100020, "US", 7, "patient-id-us"),
            (0x0020000D, "OB", None, "study-uid-ob"),
            (0x0020000E, "IS", "7", "series-uid-is"),
            (0x00100020, "SQ", Sequence([item]), "patient-id-sq")), start=2):
        ds = ct("linkage_key_not_text", study=n,
                pid=f"GOLD-linkage_key_not_text-{label}")
        if value is None:
            # The UID's own text, padded to even length with a NUL as a
            # UI is: the bytes a writer that mislabels the element leaves.
            text = str(ds[tag].value).encode("ascii")
            value = text + b"\0" * (len(text) % 2)
        ds[tag] = pydicom.DataElement(tag, vr, value)
        write(ds, out / f"linkage_key_not_text-{label}.dcm")


def sop_classes_in_the_file_meta(out: Path):
    """One CT with no SOP Class UID `(0008,0016)` in its dataset and two
    values, CT and MR Image Storage, in the file meta's Media Storage SOP
    Class UID `(0002,0002)` (#998). With no element in the dataset the
    instance's class is the worker's text of the file meta's, and that
    text is all an export has to write. Before #998 it was the text of a
    Python list, exported as one value in both elements; it is now the
    source's two values. No other input takes the worker's file-meta arm
    with more than one value."""
    ds = ct("sop_classes_in_the_file_meta")
    del ds[0x00080016]
    ds.file_meta[0x00020002] = pydicom.DataElement(
        0x00020002, "UI", [CT, "1.2.840.10008.5.1.4.1.1.4"])
    write(ds, out / "sop_classes_in_the_file_meta-1.dcm")


def label_spelling(out: Path):
    """Four files whose Photometric Interpretation `(0028,0004)` is not
    spelled as a Code String is (#612). `-no-pixels` is an Encapsulated
    PDF of its own patient, with no pixel element, declaring
    `monochrome` followed by the byte `0xDF` (`ß` in the default
    repertoire): the export respells a declared label, and until #612 did
    it by Unicode's rules, writing `MONOCHROMESS` under a `WARNING` that
    said the label was written as declared. `-icon` is a CT whose Icon
    Image Sequence item declares ` rgb`, a leading space and lower case
    over three 8-bit samples. `-whitespace` is a second Encapsulated PDF,
    of a third patient, declaring ` monochrome2` followed by a no-break
    space (the byte `0xA0`), a carriage return and a line feed: whitespace
    that is not a space, in ASCII and outside it, which the export
    removes from a label's ends and names in its note. It has no pixel
    element because that is the only way a source file brings such a
    label to the top level: over pixels, ingest refuses the file.
    `-icon-whitespace` is a second CT, of a fourth patient, whose icon
    item declares `RGB` followed by a tab: the other door. No
    other input declares a label outside the Code String repertoire, or
    one that is respelled at all. No file can hold the third edge, a
    label the export's own file cannot encode: a Code String is read
    from a file one byte to a character, so every label a file brings in
    is one a file can hold."""
    def pdf(study, name, label):
        sop = uid("label_spelling", study, 1, 1)
        ds = FileDataset(None, {}, file_meta=_meta(ENCAPSULATED_PDF, sop, ExplicitVRLittleEndian),
                         preamble=b"\0" * 128)
        ds.PatientID, ds.PatientName = f"GOLD-label_spelling-{name}", "GOLDEN^LABEL"
        ds.StudyInstanceUID = uid("label_spelling", study)
        ds.SeriesInstanceUID = uid("label_spelling", study, 1)
        ds.SOPInstanceUID, ds.SOPClassUID = sop, ENCAPSULATED_PDF
        ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "DOC", 1, 1
        ds.StudyDate, ds.StudyTime = "20230101", "120000"
        ds.ContentDate, ds.ContentTime = "20230101", "120000"
        ds.ConversionType = "WSD"
        ds.BurnedInAnnotation = "NO"
        ds.DocumentTitle = "GOLD label"
        ds.MIMETypeOfEncapsulatedDocument = "application/pdf"
        head = b"%PDF-1.4\n% GOLD label spelling\n"
        ds.EncapsulatedDocument = head + b"x" * (200 - len(head))
        ds.EncapsulatedDocumentLength = 200
        ds.add_new(0x00280004, "CS", label)
        write(ds, out / f"label_spelling-{name}.dcm")

    # No Specific Character Set, and a Code String is not under one: the
    # file holds the single byte 0xDF, and pydicom reads it back as `ß`.
    pdf(1, "no-pixels", "monochrome\xdf")
    # On the wire the label ends 0xA0 0x0D 0x0A and one pad space, which
    # pydicom removes on reading: the value read back ends with all three.
    pdf(3, "whitespace", " monochrome2\xa0\r\n")

    # `-icon-whitespace`: the same item one depth down with a tab at the
    # end of its label, four bytes on the wire.
    for study, name, label in ((2, "icon", " rgb"),
                               (4, "icon-whitespace", "RGB\t")):
        ds = ct("label_spelling", study=study,
                pid=f"GOLD-label_spelling-{name}")
        icon = Dataset()
        icon.Rows = icon.Columns = 4
        icon.SamplesPerPixel, icon.PlanarConfiguration = 3, 0
        icon.BitsAllocated = icon.BitsStored = 8
        icon.HighBit, icon.PixelRepresentation = 7, 0
        icon.add_new(0x00280004, "CS", label)
        icon.add_new(0x7FE00010, "OB", bytes(range(48)))
        ds.IconImageSequence = Sequence([icon])
        write(ds, out / f"label_spelling-{name}.dcm")


def one_bit_excess(out: Path):
    """An ordinary CT and two native 1-bit files of two other patients,
    each 3x3 under NumberOfFrames 2 (#669). A bit-packed 3x3 frame is nine
    bits, so two need three bytes and one pad. The file holding four is
    read. The file holding six has room for five such frames; pydicom
    decodes that many, a count that is not a whole number of bytes each,
    and fails in CPython's words for a slice, so the row read
    `Decompression Failed: TypeError: slice indices must be integers ...`.
    It is refused in a sentence naming the bytes held and needed.

    A fourth file, of a fourth patient: the same four bytes under a
    NumberOfFrames `(0028,0008)` that is present and holds no value.
    pydicom reads that as one frame, so the four bytes have room for
    three, and it fails the same way. It is refused in the same sentence,
    which says `NumberOfFrames is read as 1`.

    No other input holds a native 1-bit frame."""
    write(ct("one_bit_excess", study=1, pid="GOLD-one_bit_excess-good"),
          out / "one_bit_excess-good.dcm")
    for n, (held, frames, label) in enumerate(
            ((4, 2, "padded"), (6, 2, "beyond"), (4, None, "no-frames-value")),
            start=2):
        ds = ct("one_bit_excess", study=n, pid=f"GOLD-one_bit_excess-{label}")
        ds.Rows = ds.Columns = 3
        ds.NumberOfFrames = frames
        ds.BitsAllocated, ds.BitsStored, ds.HighBit = 1, 1, 0
        ds[0x7FE00010] = pydicom.DataElement(
            0x7FE00010, "OB", bytes([0b10110010, 0b01001101, 0b00000011,
                                     0, 0b01010101, 0b00001111][:held]))
        write(ds, out / f"one_bit_excess-{label}.dcm")


#: Two 4x4, 8-bit JPEG 2000 Lossless codestreams, as bytes: frames
#: `(13 * i) % 256` and `(13 * i + 100) % 256` for i in 0..15. Literals,
#: not an encoder call, so the generator needs no codec and an encoder's
#: next version cannot change what it would write.
_J2K_FRAMES = tuple(bytes.fromhex(text) for text in (
    "ff4fff5100290000000000040000000400000000000000000000000400000004"
    "00000000000000000001070101ff52000c00000001000004040001ff5c000440"
    "40ff640025000143726561746564206279204f70656e4a504547207665727369"
    "6f6e20322e352e34ff90000a0000000000230001ff93df8090120f84c8100715"
    "cde8741767895eeaf4887fffd9",
    "ff4fff5100290000000000040000000400000000000000000000000400000004"
    "00000000000000000001070101ff52000c00000001000004040001ff5c000440"
    "40ff640025000143726561746564206279204f70656e4a504547207665727369"
    "6f6e20322e352e34ff90000a0000000000220001ff93df808801178c4fa6d2c8"
    "d2006647b87f676e2190ffd9"))


def offset_table_edges(out: Path):
    """An ordinary CT and two JPEG 2000 Lossless files of two other
    patients, each two frames in two fragments under NumberOfFrames 2 and
    an empty Basic Offset Table, each with an Extended Offset Table
    (#621). One table is 12 bytes beside 12 bytes of Lengths: pydicom
    walks by it, and 12 bytes are no whole number of 8-byte offsets. It
    was refused as `Extended Offset Table names 1 frames; NumberOfFrames
    declares 2`, and is refused in a sentence naming the table's length.
    The other is 8 bytes beside 4 bytes of Lengths: pydicom drops such a
    table and reads both frames from the fragments. It was refused in
    that same sentence, and is ingested and exported.

    Four more files, of four more patients, each with a table of 8 bytes
    beside 4 bytes of Lengths, which pydicom drops. Such a table naming
    no more frames than declared is counted exactly as the same file
    without it (owner ruling 2026-10-11), and each file reaches one row
    of that:

    - `agreeing`: two JPEG 2000 frames under a Basic Offset Table of two
      offsets and NumberOfFrames 1. The table agreed with the header, so
      both frames were stored under a one-frame header, which `export()`
      cannot write. One `DATA_LOSS` row naming the Basic Offset Table,
      and frame 0.
    - `agreeing-rle`: two RLE Lossless fragments under NumberOfFrames 1.
      It was ingested as one frame with no row. One `DATA_LOSS` row
      naming the fragments.
    - `fewer-excess`: three JPEG 2000 fragments under NumberOfFrames 2.
      It was refused as `Extended Offset Table names 1 frames`. The two
      declared frames, and one `DATA_LOSS` row naming the walk's three.
    - `clause`: one JPEG 2000 fragment under NumberOfFrames 2. It was
      refused as above. It is refused as holding one fragment `with no
      offset table pydicom walks by`.

    And one more, of one more patient, `ragged-bad-header`: the 12 beside
    12 table again, under a Bits Stored of 17 beside Bits Allocated 8.
    pydicom refuses that header before it unpacks the table, and names
    the element. It was refused as `Extended Offset Table names 1 frames;
    NumberOfFrames declares 2`, and is refused in pydicom's sentence
    about Bits Stored: the ragged sentence is for a header pydicom
    accepts.

    No other input holds an Extended Offset Table."""
    write(ct("offset_table_edges", study=1, pid="GOLD-offset_table_edges-good"),
          out / "offset_table_edges-good.dcm")
    j2k, rle = "1.2.840.10008.1.2.4.90", "1.2.840.10008.1.2.5"
    one, two = _J2K_FRAMES
    for n, (label, frames, declared, table, sizes, syntax, has_bot) in enumerate((
            ("ragged", (one, two), 2, 12, 12, j2k, False),
            ("dropped", (one, two), 2, 8, 4, j2k, False),
            ("agreeing", (one, two), 1, 8, 4, j2k, True),
            ("agreeing-rle", _RLE_FRAMES, 1, 8, 4, rle, False),
            ("fewer-excess", (one, two, one), 2, 8, 4, j2k, False),
            ("clause", (one,), 2, 8, 4, j2k, False),
            ("ragged-bad-header", (one, two), 2, 12, 12, j2k, False)), start=2):
        ds = _encapsulated("offset_table_edges", n, label, frames, declared,
                           syntax, has_bot)
        if label == "ragged-bad-header":
            ds.BitsStored = 17
        ds.add_new(0x7FE00001, "OV", _true_offsets(frames)[:table])
        ds.add_new(0x7FE00002, "OV", struct.pack(
            f"<{len(frames)}Q", *(len(frame) for frame in frames))[:sizes])
        write(ds, out / f"offset_table_edges-{label}.dcm")


#: Two 4x4, 8-bit RLE Lossless frames of the same samples as
#: `_J2K_FRAMES`: the 64-byte header naming one segment at byte 64, then
#: one literal run of 16 bytes and a pad. Written out here, not encoded.
_RLE_FRAMES = tuple(
    struct.pack("<16L", 1, 64, *[0] * 14) + bytes([15])
    + bytes((13 * i + add) % 256 for i in range(16)) + b"\0"
    for add in (0, 100))


def _true_offsets(frames) -> bytes:
    """The fragments' offsets from the first, as an Extended Offset Table."""
    offsets, at = [], 0
    for frame in frames:
        offsets.append(at)
        at += 8 + len(frame) + len(frame) % 2
    return struct.pack(f"<{len(offsets)}Q", *offsets)


def _encapsulated(member, study, label, frames, declared, syntax, has_bot):
    """A 4x4, 8-bit CT of its own patient: one fragment per frame given."""
    from pydicom.encaps import encapsulate  # pylint: disable=import-outside-toplevel
    ds = ct(member, study=study, rows=4, cols=4,
            pid=f"GOLD-{member}-{label}", syntax=syntax)
    ds.NumberOfFrames = declared
    ds.BitsAllocated, ds.BitsStored, ds.HighBit = 8, 8, 7
    ds[0x7FE00010] = pydicom.DataElement(
        0x7FE00010, "OB", encapsulate(list(frames), has_bot=has_bot))
    ds[0x7FE00010].is_undefined_length = True
    return ds


def fewer_fragments_than_frames(out: Path):
    """An ordinary CT and one JPEG 2000 Lossless file of another patient:
    two frames in two fragments under NumberOfFrames 3, an empty Basic
    Offset Table and no Extended Offset Table (#683). pydicom refuses it
    with a `ValueError`, which the fallback is now tried for, so the
    row's class is pydicom's and it says what imagecodecs made of the
    file too. It read `Decompression Failed: RuntimeError: Unable to
    generate frames ...`. No other input holds fewer fragments than its
    header declares frames."""
    member = "fewer_fragments_than_frames"
    write(ct(member, study=1, pid=f"GOLD-{member}-good"),
          out / f"{member}-good.dcm")
    write(_encapsulated(member, 2, "short", _J2K_FRAMES, 3,
                        "1.2.840.10008.1.2.4.90", False),
          out / f"{member}-short.dcm")


def no_pixels_under_an_undecodable_syntax(out: Path):
    """An ordinary CT and two Encapsulated PDFs of two other patients,
    each with no pixel element, whose file meta names a transfer syntax
    no decoder implements (#1093): MPEG2 Main Profile / Main Level
    `1.2.840.10008.1.2.4.100`, and the private `1.2.840.113619.5.2`.
    Both are written as Explicit VR Little Endian bytes, which is how
    pydicom reads a dataset under either. Ingest admits them: there is
    nothing to decode. The export then asked the source for pixels,
    pydicom refused the transfer syntax before it looked for a pixel
    element, and each instance failed with `Lazy load failed for
    instance <uid>: NotImplementedError: Unable to decode the pixel data
    as a (0002,0010) 'Transfer Syntax UID' value of '...' is not
    supported`. A dataset holding none of (7FE0,0010), (7FE0,0008) and
    (7FE0,0009) has no pixels whatever the decode raised, and both are
    written. No other input with no pixel element names a transfer
    syntax pydicom has no decoder for.

    A fourth file, of a fourth patient, is the shape that rule does not
    reach: a CT header under JPIP Referenced `1.2.840.10008.1.2.4.94`
    with no pixel element and a Pixel Data Provider URL `(0028,7FE0)`.
    Its pixels are on a server, not absent, so it is not written without
    them: its export fails with the same sentence, naming `JPIP
    Referenced`, as it did before #1093 (owner ruling of 2026-10-12).
    It is here so that a change to that is seen."""
    member = "no_pixels_under_an_undecodable_syntax"
    write(ct(member, study=1, pid=f"GOLD-{member}-good"),
          out / f"{member}-good.dcm")
    for n, (label, syntax) in enumerate(
            (("mpeg2", "1.2.840.10008.1.2.4.100"),
             ("private", "1.2.840.113619.5.2")), start=2):
        sop = uid(member, n, 1, 1)
        ds = FileDataset(None, {}, file_meta=_meta(ENCAPSULATED_PDF, sop, syntax),
                         preamble=b"\0" * 128)
        ds.PatientID, ds.PatientName = f"GOLD-{member}-{label}", "GOLDEN^PDF"
        ds.StudyInstanceUID = uid(member, n)
        ds.SeriesInstanceUID = uid(member, n, 1)
        ds.SOPInstanceUID, ds.SOPClassUID = sop, ENCAPSULATED_PDF
        ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "DOC", 1, 1
        ds.StudyDate, ds.StudyTime = "20230101", "120000"
        ds.ContentDate, ds.ContentTime = "20230101", "120000"
        ds.ConversionType = "WSD"
        ds.BurnedInAnnotation = "NO"
        ds.DocumentTitle = "GOLD report"
        ds.MIMETypeOfEncapsulatedDocument = "application/pdf"
        head = b"%PDF-1.4\n% GOLDEN^PDF 1960-01-01\n"
        ds.EncapsulatedDocument = head + b"x" * (1000 - len(head))
        ds.EncapsulatedDocumentLength = 1000
        write(ds, out / f"{member}-{label}.dcm")
    ds = ct(member, study=4, pid=f"GOLD-{member}-jpip",
            syntax="1.2.840.10008.1.2.4.94")
    del ds.PixelData
    ds.PixelDataProviderURL = "http://jpip.invalid/golden?target=GOLD-jpip"
    write(ds, out / f"{member}-jpip.dcm")


def private_vr_unlike_the_dictionary(out: Path):
    """A private element whose stated VR is not the one pydicom's private
    dictionary lists (#1107). GE `GEMS_PETD_01` `(0009,109D)` is stored
    `SS` by the scanner in the MIDI-B validation set and listed `SL` by
    pydicom: `-ss` holds that shape, Explicit VR, value 0. `-sl` is the
    same element under the dictionary's own VR, the control. Until #1107
    the uncompressed export was Implicit VR, which states no VR, and
    pydicom raised `BytesLengthException` reading `-ss`'s `B.dicom`; the
    JPEG 2000 arm already wrote `SS`. Before this member no input held an
    explicit-source private element whose VR disagreed with pydicom's
    dictionary (`unstated_private_vr`'s `(0019,1012)` is a VR the source
    did not state, which is another shape)."""
    member = "private_vr_unlike_the_dictionary"
    for inst, vr in ((1, "SS"), (2, "SL")):
        ds = ct(member, inst=inst)
        ds.add_new(0x00090010, "LO", "GEMS_PETD_01")
        ds.add_new(0x0009109D, vr, 0)
        write(ds, out / f"{member}-{vr.lower()}.dcm")


#: Three codestreams of one 4x5 RGB image (`_PLANAR_RGB`), as bytes:
#: JPEG Baseline written RGB with an Adobe marker (no colour transform),
#: JPEG-LS Lossless, and RLE Lossless (an encapsulated Pixel Data value,
#: fragments and all). Literals, not encoder calls, for the reason
#: `_J2K_FRAMES` gives.
_PLANAR_JPEG = bytes.fromhex(
    "ffd8ffee000e41646f626500640000000000ffdb004300020101010101020101"
    "0102020202020403020202020504040304060506060605060606070908060709"
    "070606080b08090a0a0a0a0a06080b0c0b0a0c090a0a0affc000110800040005"
    "03521100471100421100ffc4001f000001050101010101010000000000000000"
    "0102030405060708090a0bffc400b5100002010303020403050504040000017d"
    "01020300041105122131410613516107227114328191a1082342b1c11552d1f0"
    "2433627282090a161718191a25262728292a3435363738393a43444546474849"
    "4a535455565758595a636465666768696a737475767778797a83848586878889"
    "8a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5"
    "c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8"
    "f9faffda000c03520047004200003f00ec3e097fc124bf620ff84497fe2d7ff7"
    "7fe5a27a1ff62bea8f047ec91f03ff00b247fc52fe9fc4bffc4d7b07c4dff828"
    "07ed39fdbe7fe2b8f5fe16ff00e2abffd9")
_PLANAR_JPEGLS = bytes.fromhex(
    "ffd8ffe800205350494646000200000300000004000000050a0806010000012c"
    "0000012cffe8000800000001ffd8fff70011080004000503011100021100031100"
    "ffda000c0301000200030000020040000038f2c10a4074904a5c6a480000016300"
    "0c0000c0866108433b010cc0000016310244252347682108102e406d1b54d59d80"
    "ffd9")
_PLANAR_RLE = bytes.fromhex(
    "feff00e00400000000000000feff00e088000000030000004000000058000000"
    "7000000000000000000000000000000000000000000000000000000000000000"
    "000000000000000000000000000000000000000004000a141e2804323c46505a"
    "04646e78828c0496a0aab4be0464696e7378047d82878c9104969ba0a5aa04af"
    "b4b9bec304faf3ece5de04d7d0c9c2bb04b4ada69f9804918a837c75")
#: The image the native files hold, interleaved: every channel distinct.
_PLANAR_RGB = np.stack(
    [np.arange(20).reshape(4, 5) * 10, 100 + np.arange(20).reshape(4, 5) * 5,
     250 - np.arange(20).reshape(4, 5) * 7], axis=-1).astype(np.uint8)


def no_planar_configuration(out: Path):
    """Colour images with no Planar Configuration `(0028,0006)` (#1134).
    Each is a 4x5 RGB Secondary Capture of its own patient. `-native-absent`
    (Explicit VR Little Endian, element absent) and `-native-empty` (the
    element present and zero-length) were refused at ingest with pydicom's
    `Missing required element: (0028,0006) 'Planar Configuration'`, the
    shape of 14 GE ultrasound images of the MIDI-B Validation set; each
    is now read color-by-pixel and written with 0. `-jpeg-baseline` is the
    family pydicom's Pillow plugin decodes and lays out by this element,
    `-jpegls` one the imagecodecs fallback decodes without reading it.
    `-rle` is RLE Lossless with the element absent, still refused in
    pydicom's words by the owner's ruling of 2026-10-16. `-icon` is a
    monochrome CT whose colour Icon Image Sequence item has no element: it
    was dropped with a `DATA_LOSS` row and is now carried with 0. No other
    input holds a colour image or icon with no Planar Configuration (333
    files read, top level and nested)."""
    member = "no_planar_configuration"
    sc = "1.2.840.10008.5.1.4.1.1.7"

    def image(study, label, syntax, pixels, how="absent"):
        ds = ct(member, study=study, rows=4, cols=5, syntax=syntax,
                pid=f"GOLD-{member}-{label}")
        ds.file_meta.MediaStorageSOPClassUID = ds.SOPClassUID = sc
        ds.Modality = "OT"
        ds.SamplesPerPixel = 3
        ds.PhotometricInterpretation = "RGB"
        ds.BitsAllocated, ds.BitsStored, ds.HighBit = 8, 8, 7
        if how == "empty":
            ds.add_new(0x00280006, "US", None)
        if syntax == "1.2.840.10008.1.2.4.50":
            # Stated, so the only row this member's ingest writes is RLE's.
            ds.LossyImageCompression = "01"
        ds[0x7FE00010] = pydicom.DataElement(0x7FE00010, "OB", pixels)
        if syntax != ExplicitVRLittleEndian:
            ds[0x7FE00010].is_undefined_length = True
        write(ds, out / f"{member}-{label}.dcm")

    from pydicom.encaps import encapsulate  # pylint: disable=import-outside-toplevel
    image(1, "native-absent", ExplicitVRLittleEndian, _PLANAR_RGB.tobytes())
    image(2, "native-empty", ExplicitVRLittleEndian, _PLANAR_RGB.tobytes(),
          how="empty")
    image(3, "jpeg-baseline", "1.2.840.10008.1.2.4.50",
          encapsulate([_PLANAR_JPEG], has_bot=True))
    image(4, "jpegls", "1.2.840.10008.1.2.4.80",
          encapsulate([_PLANAR_JPEGLS], has_bot=True))
    image(5, "rle", "1.2.840.10008.1.2.5", _PLANAR_RLE)

    ds = ct(member, study=6, pid=f"GOLD-{member}-icon")
    icon = Dataset()
    icon.Rows, icon.Columns, icon.SamplesPerPixel = 4, 5, 3
    icon.PhotometricInterpretation = "RGB"
    icon.BitsAllocated = icon.BitsStored = 8
    icon.HighBit, icon.PixelRepresentation = 7, 0
    icon.add_new(0x7FE00010, "OB", _PLANAR_RGB.tobytes())
    ds.IconImageSequence = Sequence([icon])
    write(ds, out / f"{member}-icon.dcm")


#: `_PLANAR_RGB` as one JPEG 2000 Lossless codestream (`jpeg2k_encode`,
#: level 0, J2K, imagecodecs 2026.8.16). A literal, for the reason
#: `_J2K_FRAMES` gives.
_PLANAR_J2K = bytes.fromhex(
    "ff4fff51002f0000000000050000000400000000000000000000000500000004"
    "00000000000000000003070101070101070101ff52000c000000010100040400"
    "01ff5c00044040ff640025000143726561746564206279204f70656e4a504547"
    "2076657273696f6e20322e352e34ff90000a00000000004e0001ff93c7d42003"
    "83c9b6df49e6c6d3cefbe2a5ca8817df80a0150d6dcc9b0e3f2cd29499ba0363"
    "1f459c703dc7cfb44c10dc2211aed6aa555bd8a90f221c76b91369d3ffd9")


def declared_planar_configuration(out: Path):
    """Colour images declaring a Planar Configuration `(0028,0006)` other
    than 0 under a compressed syntax, whose stream fixes the layout
    (#1137). Each is a 4x5 RGB Secondary Capture of `_PLANAR_RGB`, of its
    own patient. `-jpeg-baseline` (declaring 1) is the witness: pydicom's
    Pillow plugin laid it out by the element, scrambled, and it is now
    read as 0, so its exported pixels equal
    `no_planar_configuration-jpeg-baseline`'s. `-j2k` (1) is Pillow's JPEG
    2000 arm, its samples `_PLANAR_RGB`. `-jpegls` (1) is the imagecodecs
    fallback, which never read the element. `-jpeg-baseline-2` declares 2,
    refused before with pydicom's "must be 0 or 1" and read as 0 now
    (owner ruling R20a). `-icon` is a JPEG Baseline image whose Icon Image
    Sequence item is the same stream declaring 1, carried as 0. Controls,
    unchanged: `-native` holds the samples color-by-plane and declares 1,
    which a native syntax means; `-rle` declares 1, which RLE's decoder
    ignores. Every value invented."""
    member = "declared_planar_configuration"
    sc = "1.2.840.10008.5.1.4.1.1.7"
    jpeg = "1.2.840.10008.1.2.4.50"
    from pydicom.encaps import encapsulate  # pylint: disable=import-outside-toplevel

    def image(study, label, syntax, pixels, declared):
        ds = ct(member, study=study, rows=4, cols=5, syntax=syntax,
                pid=f"GOLD-{member}-{label}")
        ds.file_meta.MediaStorageSOPClassUID = ds.SOPClassUID = sc
        ds.Modality = "OT"
        ds.SamplesPerPixel = 3
        ds.PhotometricInterpretation = "RGB"
        ds.BitsAllocated, ds.BitsStored, ds.HighBit = 8, 8, 7
        ds.add_new(0x00280006, "US", declared)
        if syntax == jpeg:
            ds.LossyImageCompression = "01"
        ds[0x7FE00010] = pydicom.DataElement(0x7FE00010, "OB", pixels)
        if syntax != ExplicitVRLittleEndian:
            ds[0x7FE00010].is_undefined_length = True
        return ds

    jpeg_pixels = encapsulate([_PLANAR_JPEG], has_bot=True)
    write(image(1, "jpeg-baseline", jpeg, jpeg_pixels, 1),
          out / f"{member}-jpeg-baseline.dcm")
    write(image(2, "j2k", "1.2.840.10008.1.2.4.90",
                encapsulate([_PLANAR_J2K], has_bot=True), 1),
          out / f"{member}-j2k.dcm")
    write(image(3, "jpegls", "1.2.840.10008.1.2.4.80",
                encapsulate([_PLANAR_JPEGLS], has_bot=True), 1),
          out / f"{member}-jpegls.dcm")
    write(image(4, "jpeg-baseline-2", jpeg, jpeg_pixels, 2),
          out / f"{member}-jpeg-baseline-2.dcm")
    write(image(5, "native", ExplicitVRLittleEndian,
                np.ascontiguousarray(np.moveaxis(_PLANAR_RGB, -1, 0)).tobytes(), 1),
          out / f"{member}-native.dcm")
    write(image(6, "rle", "1.2.840.10008.1.2.5", _PLANAR_RLE, 1),
          out / f"{member}-rle.dcm")

    host = image(7, "icon", jpeg, jpeg_pixels, 0)
    icon = Dataset()
    icon.Rows, icon.Columns, icon.SamplesPerPixel = 4, 5, 3
    icon.PhotometricInterpretation = "RGB"
    icon.BitsAllocated = icon.BitsStored = 8
    icon.HighBit, icon.PixelRepresentation = 7, 0
    icon.add_new(0x00280006, "US", 1)
    icon.add_new(0x7FE00010, "OB", jpeg_pixels)
    icon[0x7FE00010].is_undefined_length = True
    host.IconImageSequence = Sequence([icon])
    write(host, out / f"{member}-icon.dcm")


COMPREHENSIVE_SR = "1.2.840.10008.5.1.4.1.1.88.33"


def _code(value, scheme, meaning):
    code = Dataset()
    code.CodeValue, code.CodingSchemeDesignator, code.CodeMeaning = value, scheme, meaning
    return code


def _content_item(value_type, concept, relationship="CONTAINS", **values):
    item = Dataset()
    item.RelationshipType, item.ValueType = relationship, value_type
    item.ConceptNameCodeSequence = Sequence([_code(*concept)])
    for keyword, value in values.items():
        setattr(item, keyword, value)
    return item


def structured_report(out: Path):
    """A dcmqi-style Comprehensive SR (#1131): a TEXT holding an injected
    ID, a tracking identifier and two provenance strings kept verbatim
    (#1131 quotes `Nodule 1`, `pylidc`, `0.2.0`), a PNAME observer, a
    DATE, a NUM with units and a UIDREF, and Study and Series Descriptions
    holding an ID and none. A and B remove the Content Sequence
    (`basic@2026c`, #848) and the descriptions; C keeps the tree and
    scrubs the Text Values and the descriptions (#1130). Values invented."""
    sop = uid("structured_report", 1, 1, 1)
    ds = FileDataset(None, {}, file_meta=_meta(COMPREHENSIVE_SR, sop, ExplicitVRLittleEndian),
                     preamble=b"\0" * 128)
    ds.SpecificCharacterSet = "ISO_IR 100"
    ds.PatientID, ds.PatientName = "GOLD-structured_report", "GOLDEN^REPORT"
    ds.PatientBirthDate, ds.PatientSex = "19600101", "F"
    ds.StudyInstanceUID = uid("structured_report", 1)
    ds.SeriesInstanceUID = uid("structured_report", 1, 1)
    ds.SOPInstanceUID, ds.SOPClassUID = sop, COMPREHENSIVE_SR
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "SR", 1, 1
    ds.StudyDate, ds.ContentDate, ds.StudyTime = "20230101", "20230101", "120000"
    ds.StudyID, ds.AccessionNumber = "S1", "ACC1"
    ds.ReferringPhysicianName = "REF^DOC"
    ds.StudyDescription = "CHEST for 8675309421"
    ds.SeriesDescription = "Lesion 1 - Reader 2"
    ds.Manufacturer = "Acme"
    ds.ValueType = "CONTAINER"
    ds.ConceptNameCodeSequence = Sequence([_code("126000", "DCM",
                                                 "Imaging Measurement Report")])
    ds.ContinuityOfContent, ds.CompletionFlag = "SEPARATE", "COMPLETE"
    ds.VerificationFlag = "UNVERIFIED"
    measured = Dataset()
    measured.NumericValue = "71.06"
    measured.MeasurementUnitsCodeSequence = Sequence([_code("mm", "UCUM", "millimeter")])
    ds.ContentSequence = Sequence([
        _content_item("TEXT", ("121106", "DCM", "Comment"),
                      TextValue="GOLD MASS for 8675309421"),
        _content_item("TEXT", ("112039", "DCM", "Tracking Identifier"),
                      TextValue="Nodule 1"),
        _content_item("TEXT", ("111526", "DCM", "Algorithm Name"), TextValue="pylidc"),
        _content_item("TEXT", ("111528", "DCM", "Algorithm Version"), TextValue="0.2.0"),
        _content_item("PNAME", ("121008", "DCM", "Person Observer Name"),
                      relationship="HAS OBS CONTEXT", PersonName="OBSERVER^ONE"),
        _content_item("DATE", ("111060", "DCM", "Study Date"), Date="20221111"),
        _content_item("NUM", ("G-D705", "SRT", "Volume"),
                      MeasuredValueSequence=Sequence([measured])),
        _content_item("UIDREF", ("121232", "DCM", "Source series for segmentation"),
                      UID=uid("structured_report", "source series")),
    ])
    write(ds, out / "structured_report-1.dcm")


def private_creators(out: Path):
    """GE's `GEMS_IDEN_01` "Image actual date" (offset 27) in block 10 of
    one file and block 11 of another, and `GEMS_PETD_01` holding a
    parameter at `0009,1027`, the tag the first file's date uses (#1132).
    A sweeps all three; B keeps all three; C's creator-scoped REMOVE
    removes the two dates and keeps the parameter. Values invented."""
    first = ct("private_creators", inst=1)
    first.private_block(0x0009, "GEMS_IDEN_01", create=True).add_new(0x27, "LO", "20230101")
    write(first, out / "private_creators-1.dcm")
    second = ct("private_creators", inst=2)
    second.private_block(0x0009, "GEMS_PETD_01", create=True).add_new(0x27, "LO", "1.5")
    second.private_block(0x0009, "GEMS_IDEN_01", create=True).add_new(0x27, "LO", "20230101")
    write(second, out / "private_creators-2.dcm")


MEMBERS = {f.__name__: f for f in (
    longitudinal, private_nested, redacted, curve_overlay, implicit, ecg,
    lut_ambiguous, big_endian_words, float_pixels, no_study_date,
    no_patient_id, withheld, prior_markers, graphic_annotation, big_lut,
    encapsulated_pdf, unstated_private_vr,
    multi_valued_keys, lut_unusable_descriptor, multi_valued_pn,
    ecg_lead_codes, number_vr_on_a_date_or_time,
    binary_number_vr_on_a_text_tag, multi_valued_series_fields,
    source_says_unmodified, linkage_key_not_text,
    sop_classes_in_the_file_meta, label_spelling, one_bit_excess,
    offset_table_edges, fewer_fragments_than_frames,
    no_pixels_under_an_undecodable_syntax, private_vr_unlike_the_dictionary,
    no_planar_configuration, structured_report, private_creators,
    declared_planar_configuration)}


def build(out: Path = COHORT, only=None) -> list:
    """Write every missing member under `out`; return the names written."""
    written = []
    for name, builder in MEMBERS.items():
        if only and name not in only:
            continue
        target = out / name
        if target.exists():
            print(f"{name}: exists, left as committed", file=sys.stderr)
            continue
        target.mkdir(parents=True)
        builder(target)
        written.append(name)
        print(f"{name}: written", file=sys.stderr)
    return written


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.golden_cohort",
                                     description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", default=str(COHORT))
    parser.add_argument("--only", help="comma-separated member names")
    args = parser.parse_args(argv)
    build(Path(args.out), set(args.only.split(",")) if args.only else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())

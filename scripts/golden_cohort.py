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
    """Four ECGs of four patients whose Channel Source codes the WFDB lead
    names turn on (#832). `-2008-12lead`: twelve leads coded to PS3.16's
    2008 lead table, where `MDC 2:3` is both Lead III and Lead V1, with a
    mark on each (channels 3 and 7); the Code Meaning tells them apart.
    `-2008-limb`: leads I, II and III of that table, a `2:3` with no V1
    beside it. `-padded`: Code Values with a space in front, behind and on
    both sides. `-coded-twice`: Lead II on two channels, so both are
    written as `2:2`, with the `WARNING` row and a mark on channel 2. The
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
            ("2:2", "Lead II", "II"), ("2:2", "Lead II", "II"))))
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
            fds = FileDataset(None, ds, file_meta=ds.file_meta, preamble=b"\0" * 128)
            write(fds, out / f"ecg_lead_codes-{label}.dcm", ExplicitVRLittleEndian)


MEMBERS = {f.__name__: f for f in (
    longitudinal, private_nested, redacted, curve_overlay, implicit, ecg,
    lut_ambiguous, big_endian_words, float_pixels, no_study_date,
    no_patient_id, withheld, prior_markers, graphic_annotation, big_lut,
    encapsulated_pdf, unstated_private_vr,
    multi_valued_keys, lut_unusable_descriptor,
    ecg_lead_codes)}


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

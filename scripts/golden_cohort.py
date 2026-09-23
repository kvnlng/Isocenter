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


MEMBERS = {f.__name__: f for f in (
    longitudinal, private_nested, redacted, curve_overlay, implicit, ecg,
    lut_ambiguous, big_endian_words, float_pixels, no_study_date,
    no_patient_id, withheld, prior_markers)}


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

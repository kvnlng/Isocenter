"""A study date that was never recorded must stay absent.

Ingest filled a missing Study Date with the sentinel `19000101`, and
nothing downstream could tell that apart from a real date. `SHIFT_DATE`
jittered it like any other, so an instance that never had a date was
exported carrying a confidently de-identified acquisition date somewhere
near 1900 -- in the folder tree, in the WFDB header comment, and in any
date-based cohort filter.

That is invention, not leakage, and it is the more insidious of the two:
a consumer cannot distinguish "acquired in 1899, de-identified" from "we
never knew". These tests come in through `session.ingest()`, because the
sentinel is applied there and every export path inherits it. (#60)
"""
import pathlib
from datetime import date

import pydicom
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian

from isocenter.session import DicomSession
from isocenter.io_handlers import export_folder_names

CT = "1.2.840.10008.5.1.4.1.1.2"


def _write(directory, name, study_date=None, uid_suffix="1"):
    """A minimal readable DICOM, with Study Date included only if given."""
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = CT
    meta.MediaStorageSOPInstanceUID = f"1.2.3.{uid_suffix}"
    meta.TransferSyntaxUID = ExplicitVRLittleEndian

    ds = Dataset()
    ds.file_meta = meta
    ds.is_little_endian = True
    ds.is_implicit_VR = False
    ds.SOPInstanceUID = f"1.2.3.{uid_suffix}"
    ds.SOPClassUID = CT
    ds.StudyInstanceUID = f"1.2.4.{uid_suffix}"
    ds.SeriesInstanceUID = f"1.2.5.{uid_suffix}"
    ds.PatientID = "P1"
    ds.PatientName = "Test^Patient"
    ds.Modality = "OT"
    ds.SeriesNumber = 1
    if study_date is not None:
        ds.StudyDate = study_date

    path = pathlib.Path(directory) / name
    ds.save_as(str(path), enforce_file_format=True)
    return path


@pytest.fixture
def ingested(tmp_path):
    """Ingests a directory built by the caller and yields the session."""
    sessions = []

    def run(build):
        source = tmp_path / f"src{len(sessions)}"
        source.mkdir()
        build(source)
        session = DicomSession(":memory:")
        sessions.append(session)
        session.ingest(str(source))
        return session

    yield run
    for session in sessions:
        session.close()


def test_a_file_with_no_study_date_yields_no_study_date(ingested):
    """`date(1900, 1, 1)` is not a date this study has."""
    session = ingested(lambda d: _write(d, "a.dcm"))

    study = session.store.patients[0].studies[0]

    assert study.study_date is None, (
        f"ingest invented a study date: {study.study_date!r}")


def test_an_unparseable_study_date_is_not_replaced_with_1900(ingested):
    """The parse fallback was the same sentinel by another route."""
    session = ingested(lambda d: _write(d, "a.dcm", study_date="NOTADATE"))

    study = session.store.patients[0].studies[0]

    assert study.study_date != date(1900, 1, 1), (
        "an unreadable date was replaced with a sentinel that reads as real")
    assert study.study_date is None


def test_a_real_study_date_still_survives_ingest(ingested):
    """The fix must not lose dates that were actually recorded."""
    session = ingested(lambda d: _write(d, "a.dcm", study_date="20230417"))

    assert session.store.patients[0].studies[0].study_date == date(2023, 4, 17)


def test_a_missing_date_is_not_offered_for_shifting(ingested):
    """Shifting nothing produces a date, which is the whole defect.

    `SHIFT_DATE` jittered the sentinel and wrote the result back as the
    study's de-identified date.
    """
    session = ingested(lambda d: _write(d, "a.dcm"))
    session.configuration.phi_tags = {
        "0008,0020": {"name": "Study Date", "action": "SHIFT"}}

    report = session.audit()
    session.anonymize()

    assert not [f for f in report.findings if f.tag == "0008,0020"], (
        "a study with no date was flagged for date shifting")
    assert session.store.patients[0].studies[0].study_date is None


def test_the_export_folder_says_there_was_no_date(ingested):
    """`export_folder_names` has a "NoDate" branch that was unreachable.

    Every study arrived carrying a parsed sentinel, so the exported tree
    filed undated studies under a 1900 date instead.
    """
    session = ingested(lambda d: _write(d, "a.dcm"))
    patient = session.store.patients[0]
    study = patient.studies[0]

    _subject, study_folder, _series = export_folder_names(
        patient, study, study.series[0])

    assert "NoDate" in study_folder, (
        f"undated study exported under {study_folder!r}")
    assert "1900" not in study_folder


def test_an_absent_date_survives_a_save_and_reload(tmp_path):
    """The store must not reintroduce what ingest stopped inventing.

    `study_date` is a column like any other; a round trip that turned
    NULL back into a date -- or into the string "None" -- would put the
    sentinel back by a different route, and the export paths would never
    know.
    """
    source = tmp_path / "src"
    source.mkdir()
    _write(source, "a.dcm")

    db = tmp_path / "roundtrip.db"
    session = DicomSession(str(db))
    try:
        session.ingest(str(source))
        assert session.store.patients[0].studies[0].study_date is None
        session.save()
    finally:
        session.close()

    reopened = DicomSession(str(db))
    try:
        study = reopened.store.patients[0].studies[0]
        assert study.study_date is None, (
            f"the store returned {study.study_date!r} for a study that has "
            "no date")
    finally:
        reopened.close()

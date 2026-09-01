"""`Study.date_shifted` must survive a store round trip. (#182)

SHIFT_DATE writes the jittered date onto `study.study_date` and sets
`study.date_shifted = True` (`remediation.py`); `WfdbExporter
._start_datetime` reads the flag to decide whether the header's date
comment may say "de-identified". The `studies` table had no column for
it, so every save dropped the flag and a reloaded study reported
`date_shifted=False` whether or not `anonymize()` ever ran. #171 fixed
the value half (the date itself now round-trips as a `date`); this is
the provenance half: the reloaded export wrote `start date:` where the
fresh one wrote `de-identified start date:` -- a genuine
de-identification the export declined to claim.

The migration direction is pinned by the last test: a store created
before the column existed opens cleanly (guarded ALTER, the same
pattern as `phi_status`) and reads `date_shifted=False`. False is the
honest answer there, not a loss: the old schema never held the
information, and understating is the failure direction the WFDB comment
exists to keep -- it must never assert a de-identification it cannot
show.

`Study.study_time` and `Instance.date_shifted` have the same absence
(see #182's sweep comment) and are deliberately not covered here; the
design call for those is still open.
"""
import pathlib
import sqlite3

import pydicom
import pytest

from isocenter.session import DicomSession


def _ecg_with_date(tmp_path, study_date="20230417"):
    """An ECG carrying a Study Date and no time-of-day.

    Times are stripped so `_start_datetime` takes the comment path --
    the one place `date_shifted` is read -- rather than combining the
    date into a record-line datetime.
    """
    from scripts.generate_waveform_test_data import build_ecg_dataset

    ds = build_ecg_dataset(channels=[("MDC_ECG_LEAD_I", "Lead I")],
                           patient_id="SHIFTFLAG001",
                           patient_name="Waveform^Test")
    ds.StudyDate = study_date
    for tag in ("StudyTime", "AcquisitionDateTime", "ContentTime",
                "AcquisitionTime"):
        if tag in ds:
            delattr(ds, tag)

    path = tmp_path / "wf.dcm"
    pydicom.dcmwrite(str(path), ds, write_like_original=False)
    return path


def _ingest_and_save(tmp_path, anonymize):
    """Ingests the ECG, optionally anonymizes, saves, and closes.

    Returns the store path. The pre-save flag is asserted here so the
    round-trip tests below measure the store, not the fixture.
    """
    db_path = str(tmp_path / "s.db")
    session = DicomSession(persistence_file=db_path)
    try:
        session.ingest(str(tmp_path))
        if anonymize:
            config = tmp_path / "config.yaml"
            session.create_config(str(config))
            session.load_config(str(config))
            session.anonymize(session.audit())
        shifted = session.store.patients[0].studies[0].date_shifted
        assert shifted is anonymize, (
            "test setup: anonymize() did not produce the expected flag")
        session.save(sync=True)
    finally:
        session.close()
    return db_path


def test_date_shifted_survives_a_save_and_reload(tmp_path):
    """The flag SHIFT_DATE set must still be there after a reload."""
    _ecg_with_date(tmp_path)
    db_path = _ingest_and_save(tmp_path, anonymize=True)

    reopened = DicomSession(persistence_file=db_path)
    try:
        study = reopened.store.patients[0].studies[0]
        assert study.date_shifted is True, (
            "the store dropped date_shifted: the reloaded session cannot "
            "tell a shifted date from an original one")
    finally:
        reopened.close()


def test_an_unshifted_study_reloads_as_unshifted(tmp_path):
    """The control: persisting the flag must not invent a shift."""
    _ecg_with_date(tmp_path)
    db_path = _ingest_and_save(tmp_path, anonymize=False)

    reopened = DicomSession(persistence_file=db_path)
    try:
        assert reopened.store.patients[0].studies[0].date_shifted is False
    finally:
        reopened.close()


def test_a_reloaded_wfdb_export_keeps_the_de_identified_qualifier(tmp_path):
    """The consumer-visible half: the header's provenance claim.

    Before the column, the fresh export wrote `de-identified start
    date:` and the reloaded one wrote `start date:` for the same store
    -- a consumer reading provenance out of the header got a different
    answer depending on whether the exporting session happened to be
    reopened.
    """
    _ecg_with_date(tmp_path)
    db_path = _ingest_and_save(tmp_path, anonymize=True)

    reopened = DicomSession(persistence_file=db_path)
    try:
        paths = reopened.export(str(tmp_path / "out"), format="wfdb")
    finally:
        reopened.close()
    header = pathlib.Path(paths[0]).read_text(encoding="utf-8")

    assert "de-identified start date" in header, (
        "the reloaded export lost the qualifier; the date really was "
        "shifted and the header must still say so:\n" + header)


def test_a_store_created_before_the_column_existed_still_opens(tmp_path):
    """Upgrading must not require rebuilding the session.

    `CREATE TABLE IF NOT EXISTS` leaves an existing table untouched, so
    the column never appears in a store an earlier version created. The
    guarded ALTER adds it; rows predating it read NULL, which hydrates
    as False -- correct, because the old schema never recorded whether
    a date was shifted, and a claim the store cannot back must not be
    made (the same direction the WFDB header comment enforces).
    """
    _ecg_with_date(tmp_path)
    db_path = _ingest_and_save(tmp_path, anonymize=True)

    with sqlite3.connect(db_path) as conn:
        conn.execute("ALTER TABLE studies DROP COLUMN date_shifted")

    reopened = DicomSession(persistence_file=db_path)
    try:
        study = reopened.store.patients[0].studies[0]
        assert study.date_shifted is False, (
            "a pre-column store never held the flag; hydrating True "
            "would fabricate provenance")
    finally:
        reopened.close()

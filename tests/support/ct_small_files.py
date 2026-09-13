"""CT_small copies with chosen identifiers, and the store's row counts.

For the #548 / #551 / #552 tests, which ingest real files so the whole
ingest -> audit -> anonymize -> save path runs. In `tests/support/` for
the reason `project_secret.py` gives: pytest collects only `test_*.py` at
the top of `tests/`. No `isocenter` import, so the mutation probe charges
coverage to the tests that use this rather than to this helper.
"""
import os
import sqlite3

import pydicom
from pydicom.data import get_testdata_file

#: A UID root no real file carries, so a study is named by its suffix.
ROOT = "1.2.826.0.1.3680043.10.9999"

#: Tables in graph order, which is the order `row_counts` returns.
TABLES = ("patients", "studies", "series", "instances")


def study_uid(suffix):
    """The Study Instance UID `write_ct` gives `suffix`."""
    return f"{ROOT}.{suffix}"


def write_ct(path, patient_id, suffix, study_date="keep", name=None):
    """Write CT_small under one study, one series and one instance.

    `study_date="keep"` leaves CT_small's own StudyDate; `None` deletes
    the element, so the scan raises no SHIFT_DATE and the study stays
    clean through a pass; any other value is written as the DA string.
    """
    ds = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    ds.PatientID = patient_id
    ds.PatientName = name or f"Test^{patient_id}"
    ds.StudyInstanceUID = study_uid(suffix)
    ds.SeriesInstanceUID = f"{study_uid(suffix)}.1"
    ds.SOPInstanceUID = f"{study_uid(suffix)}.1.1"
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    if study_date is None:
        if "StudyDate" in ds:
            del ds.StudyDate
    elif study_date != "keep":
        ds.StudyDate = study_date
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    ds.save_as(str(path))
    return str(path)


def row_counts(db_path):
    """(patients, studies, series, instances) rows in the store at `db_path`."""
    with sqlite3.connect(str(db_path)) as conn:
        return tuple(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                     for t in TABLES)


def stored_study_uids(db_path):
    """Every Study Instance UID with a row in the store."""
    with sqlite3.connect(str(db_path)) as conn:
        return {r[0] for r in conn.execute(
            "SELECT study_instance_uid FROM studies")}


def stored_study_dates(db_path):
    """Study Instance UID -> the study_date column, as stored."""
    with sqlite3.connect(str(db_path)) as conn:
        return dict(conn.execute(
            "SELECT study_instance_uid, study_date FROM studies").fetchall())

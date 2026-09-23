"""An id in `patient_ids` that names no patient is counted, never named (#686).

Measured at 042aa01f, identically on 3.12 and 3.14t:
`export(patient_ids=["NOT-A-PATIENT"])` wrote nothing and graded `PASS`,
and `export(patient_ids=["COH-A", "NOT-A-PATIENT"])` wrote `COH-A` and
graded `PASS` -- a short export that reads as a complete one, on both
formats, with nothing logged. The common way to get there is not a typo:
after `anonymize()` every Patient ID is its replacement, and a list built
from the source IDs selected nobody.

Owner ruling Q1 on #686: counted. The matching patients are exported as
before; the others are counted by position, never named, in one `WARNING`
log line on every door, and on `export` one `WARNING` audit row, which
grades the report `REVIEW_REQUIRED` (Q3). The lock doors keep their own
`ERROR` count (`test_every_door_selects_patients_one_way.py`).
"""
import logging
import os
import sqlite3

import pytest

from isocenter.session import DicomSession

A, B = "COH-A", "COH-B"
UNKNOWN_1, UNKNOWN_2 = "NOT-A-PATIENT", "ALSO-NOT"
SENTENCE = "no patient in the session matches"


def _session(tmp_path, name):
    from scripts.generate_waveform_test_data import write_fixture

    source = tmp_path / f"src_{name}"
    source.mkdir()
    write_fixture(str(source / "a.dcm"), num_samples=64, patient_id=A,
                  patient_name="Alpha^Ann")
    write_fixture(str(source / "b.dcm"), num_samples=64, patient_id=B,
                  patient_name="Beta^Bob")
    session = DicomSession(persistence_file=str(tmp_path / f"{name}.db"))
    session.ingest(str(source))
    session.save(sync=True)
    assert {p.patient_id for p in session.store.patients} == {A, B}
    return session


def _rows(session):
    session.store_backend.flush_audit_queue()
    with sqlite3.connect(session.store_backend.db_path) as conn:
        return conn.execute("SELECT action_type, entity_uid, details "
                            "FROM audit_log ORDER BY id").fetchall()


def _count_rows(rows):
    return [row for row in rows if SENTENCE in (row[2] or "")]


def _owner_by_uid(session):
    return {inst.sop_instance_uid: p.patient_id
            for p in session.store.patients for st in p.studies
            for se in st.series for inst in se.instances}


def _export(session, folder, fmt, selection, **options):
    """The Patient IDs an export wrote."""
    if fmt == "dicom":
        owner = _owner_by_uid(session)
        summary = session.export(str(folder), patient_ids=selection,
                                 show_progress=False, **options)
        return {owner[uid] for uid in summary.written_uids}
    written = session.export(str(folder), format="wfdb", patient_ids=selection)
    return {os.path.basename(path).split("_")[0] for path in written}


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING and SENTENCE in r.getMessage()]


def _grade(session, tmp_path):
    path = tmp_path / "report.md"
    session.generate_report(str(path))
    line = next(line for line in path.read_text().splitlines()
                if "Validation Status" in line)
    return "REVIEW_REQUIRED" if "REVIEW_REQUIRED" in line else line


# --- U1: the read doors log, and write no row -------------------------------------

@pytest.mark.parametrize("door", ["cohort_report", "export_dataframe"])
def test_a_read_door_counts_unmatched_ids(tmp_path, door, caplog):
    with _session(tmp_path, door) as session:
        before = len(_rows(session))
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            if door == "cohort_report":
                frame = session.get_cohort_report(
                    patient_ids=[A, UNKNOWN_1, UNKNOWN_2])
            else:
                frame = session.export_dataframe(
                    str(tmp_path / "f.csv"), patient_ids=[A, UNKNOWN_1, UNKNOWN_2])
        after = len(_rows(session))
    assert set(frame["PatientID"]) == {A}
    counted = _warnings(caplog)
    assert len(counted) == 1, caplog.messages
    assert "2 of the 3 ids given" in counted[0], counted[0]
    assert "positions 2, 3" in counted[0], counted[0]
    for identifier in (A, B, UNKNOWN_1, UNKNOWN_2):
        assert all(identifier not in m for m in caplog.messages), identifier
    assert after == before, "a read door wrote an audit row"


# --- U2, U3: the export doors write one row, and the grade reads it ---------------

@pytest.mark.parametrize("fmt", ["dicom", "wfdb"])
def test_an_export_with_unmatched_ids_writes_one_warning_row(tmp_path, fmt, caplog):
    folder = tmp_path / "out"
    with _session(tmp_path, fmt) as session:
        before = len(_rows(session))
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            written = _export(session, folder, fmt, [A, UNKNOWN_1, UNKNOWN_2])
        added = _rows(session)[before:]
        grade = _grade(session, tmp_path)
    assert written == {A}, "the matching patient was not exported (best-effort)"
    counted = _count_rows(added)
    assert len(counted) == 1, added
    action, entity_uid, details = counted[0]
    assert action == "WARNING" and entity_uid == str(folder), counted[0]
    prefix = "DICOM export to " if fmt == "dicom" else "WFDB export to "
    assert details.startswith(prefix), details
    assert "2 of the 3 ids given" in details and "positions 2, 3" in details
    for identifier in (A, B, UNKNOWN_1, UNKNOWN_2):
        assert identifier not in details, identifier
        assert all(identifier not in m for m in _warnings(caplog)), identifier
    assert len(_warnings(caplog)) == 1, caplog.messages
    exports = [row for row in added if row[0] == "EXPORT"]
    assert len(exports) == 1 and SENTENCE not in exports[0][2], exports
    assert grade == "REVIEW_REQUIRED", grade


@pytest.mark.parametrize("fmt", ["dicom", "wfdb"])
def test_a_full_export_still_grades_pass(tmp_path, fmt):
    """The control for the grade above: the same fixture, every id known."""
    with _session(tmp_path, fmt) as session:
        assert _export(session, tmp_path / "out", fmt, [A]) == {A}
        grade = _grade(session, tmp_path)
    assert "**PASS**" in grade, grade


# --- U4: the worked example ---------------------------------------------------------

def test_the_source_id_after_anonymize_is_counted(tmp_path, caplog):
    """After `anonymize()` a patient is selected by its replacement ID; the
    source ID selects nobody, and says so. Also pins the singular form."""
    with _session(tmp_path, "anon") as session:
        session.anonymize(session.audit())
        before = len(_rows(session))
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            written = _export(session, tmp_path / "out", "dicom", [A])
        added = _rows(session)[before:]
        grade = _grade(session, tmp_path)
    assert written == set()
    counted = _count_rows(added)
    assert len(counted) == 1, added
    assert "1 of the 1 id given (position 1," in counted[0][2], counted[0][2]
    assert "replacement Patient ID" in counted[0][2], counted[0][2]
    assert grade == "REVIEW_REQUIRED", grade


# --- U5, U6: when no row is owed ---------------------------------------------------

@pytest.mark.parametrize("selection", [[A], [], None], ids=["known", "empty", "None"])
@pytest.mark.parametrize("fmt", ["dicom", "wfdb"])
def test_no_row_when_every_id_matches_or_none_is_given(tmp_path, fmt, selection, caplog):
    with _session(tmp_path, fmt) as session:
        before = len(_rows(session))
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            _export(session, tmp_path / "out", fmt, selection)
        added = _rows(session)[before:]
    assert _count_rows(added) == [], added
    assert _warnings(caplog) == [], caplog.messages


def test_all_withheld_export_with_an_unknown_id_still_writes_the_row(tmp_path):
    """The row sits before *both* empty-plan branches. A CT never
    anonymized, exported with `check_burned_in=True`, has its every
    instance withheld, and that branch returns early: the unknown id must
    still be counted there, not only on the empty-plan path (review of
    #696, M6)."""
    import pydicom  # pylint: disable=import-outside-toplevel
    from pydicom.data import get_testdata_file  # pylint: disable=import-outside-toplevel

    source = tmp_path / "in"
    source.mkdir()
    ds = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    ds.PatientID = "CT-A"
    ds.PatientName = "Doe^Jane"
    ds.save_as(str(source / "a.dcm"))
    with DicomSession(persistence_file=str(tmp_path / "ct.db")) as session:
        session.ingest(str(source))
        summary = session.export(str(tmp_path / "out"),
                                 patient_ids=["CT-A", UNKNOWN_1],
                                 check_burned_in=True, show_progress=False)
        rows = _rows(session)
    assert summary.written == 0
    assert any(row[0] == "EXPORT" and "were withheld" in row[2] for row in rows), (
        "the fixture did not take the all-withheld branch; the assertion "
        f"below would not test it: {rows}")
    counted = _count_rows(rows)
    assert len(counted) == 1, rows
    assert "1 of the 2 ids given (position 2," in counted[0][2], counted[0][2]


def test_no_row_for_an_export_that_did_not_run(tmp_path):
    """A `subset` refused after the selection was read: the export never
    ran, so no row may say it selected short."""
    with _session(tmp_path, "norun") as session:
        before = len(_rows(session))
        with pytest.raises(TypeError):
            session.export(str(tmp_path / "out"), patient_ids=[UNKNOWN_1],
                           subset=[42], show_progress=False)
        added = _rows(session)[before:]
    assert _count_rows(added) == [], added


# --- U7, U8, U9: the sentence's arithmetic -----------------------------------------

def test_positions_are_capped_at_ten(tmp_path, caplog):
    unknown = [f"NOPE-{n:02d}" for n in range(12)]
    with _session(tmp_path, "cap") as session:
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            session.get_cohort_report(patient_ids=[A] + unknown)
    (message,) = _warnings(caplog)
    assert "12 of the 13 ids given" in message, message
    assert "(positions 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, and 2 more," in message, message


def test_the_store_door_does_not_count(tmp_path, caplog):
    """`get_flattened_instances` is a paged query with no single moment at
    which an id is or is not held: it answers, and does not count."""
    with _session(tmp_path, "store") as session:
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            rows = list(session.store_backend.get_flattened_instances(["NOPE"]))
    assert rows == []
    assert _warnings(caplog) == [], caplog.messages


def test_a_duplicated_unknown_id_counts_at_each_position(tmp_path, caplog):
    with _session(tmp_path, "dup") as session:
        with caplog.at_level(logging.WARNING, logger="isocenter"):
            session.get_cohort_report(patient_ids=["NOPE", A, "NOPE"])
    (message,) = _warnings(caplog)
    assert "2 of the 3 ids given" in message, message
    assert "positions 1, 3," in message, message

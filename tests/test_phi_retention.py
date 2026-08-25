"""The session store must not keep what the session removed.

`session.export()` writes de-identified copies. The session's own
database is a different artefact, and it is the one that sits on disk
afterwards -- so anything it retains is retained in the place a
researcher is most likely to still have.

These tests cover what happens to a row when the entity it describes
changes identity or disappears from memory. Instances already had this
rule; nothing above them did.
"""
import datetime
import sqlite3

import pytest

from isocenter.builders import DicomBuilder
from isocenter.entities import Patient, Study, Series, Instance
from isocenter.persistence import SqliteStore
from isocenter.session import DicomSession


@pytest.fixture
def session(tmp_path):
    sess = DicomSession(str(tmp_path / "session"))
    patient = (DicomBuilder.start_patient("P123", "John Doe")
               .add_study("S1", datetime.date(2023, 1, 1))
               .add_series("SE1", "CT", 1)
               .add_instance("I1", "1.2.3", 1)
               .end_instance().end_series().end_study().build())
    sess.store.patients.append(patient)
    yield sess
    sess.close()


def patient_rows(db_path):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT patient_id, patient_name FROM patients").fetchall()


def test_anonymising_leaves_no_trace_of_the_original_patient(session):
    """The row the patient used to be must not survive the rename.

    `save_all` upserts patients on `patient_id`. Anonymisation changes
    that value, so the write inserted a *second* row and orphaned the
    first -- name and identifier intact. The studies were re-parented to
    the new row, so nothing ever visited the old one again and no
    deletion reached it. Reopening the session showed two patients: the
    anonymised one, and 'P123 / John Doe' with no studies.
    """
    session.save(sync=True)
    session.anonymize()
    session.save(sync=True)

    rows = patient_rows(session.store_backend.db_path)

    assert len(rows) == 1, f"the original patient row survived: {rows}"
    identifiers = {value for row in rows for value in row}
    assert "P123" not in identifiers
    assert "John Doe" not in identifiers


def test_a_reopened_session_shows_only_the_anonymised_patient(session):
    """What is on disk is what the next session loads."""
    session.save(sync=True)
    session.anonymize()
    session.save(sync=True)
    db_path = session.store_backend.db_path
    session.close()

    reopened = DicomSession(db_path)
    try:
        assert len(reopened.store.patients) == 1
        assert reopened.store.patients[0].patient_name != "John Doe"
    finally:
        reopened.close()


def test_a_study_dropped_from_memory_is_deleted_from_the_database(session):
    """Deletion applies at every level, not only to instances."""
    patient = session.store.patients[0]
    patient.studies.append(Study("S2", "20230202"))
    session.save(sync=True)

    patient.studies = [s for s in patient.studies
                       if s.study_instance_uid != "S2"]
    session.save(sync=True)

    with sqlite3.connect(session.store_backend.db_path) as conn:
        stored = {r[0] for r in conn.execute(
            "SELECT study_instance_uid FROM studies").fetchall()}

    assert "S2" not in stored
    assert "S1" in stored


def test_a_series_dropped_from_memory_is_deleted_from_the_database(session):
    """Same rule, one level down."""
    study = session.store.patients[0].studies[0]
    study.series.append(Series("SE2", "MR", 2))
    session.save(sync=True)

    study.series = [s for s in study.series
                    if s.series_instance_uid != "SE2"]
    session.save(sync=True)

    with sqlite3.connect(session.store_backend.db_path) as conn:
        stored = {r[0] for r in conn.execute(
            "SELECT series_instance_uid FROM series").fetchall()}

    assert "SE2" not in stored
    assert "SE1" in stored


def test_saving_some_patients_does_not_delete_the_others(tmp_path):
    """Pruning is only safe when the caller owns the whole store.

    `save_all` is public and accepts any list. Deleting every patient
    absent from that list would turn an incremental save of one patient
    into the deletion of everyone else, which is a far worse failure than
    the leak this pruning exists to close.
    """
    store = SqliteStore(str(tmp_path / "partial.db"))

    def make(pid):
        patient = Patient(pid, f"Name {pid}")
        study = Study(f"{pid}.S", "20230101")
        series = Series(f"{pid}.SE", "CT", 1)
        series.instances.append(Instance(f"{pid}.I", "1.2.3"))
        study.series.append(series)
        patient.studies.append(study)
        return patient

    first, second = make("P1"), make("P2")
    store.save_all([first, second])

    store.save_all([first])          # an ordinary partial save

    assert {row[0] for row in patient_rows(store.db_path)} == {"P1", "P2"}


def test_removing_a_phi_tag_marks_the_instance_for_saving(session):
    """A removal that is never written is not a removal.

    `REMOVE_TAG` deletes straight out of `entity.attributes`, a plain
    dict -- unlike `set_attr`, deleting from it bumps no revision. An
    instance that had already been saved therefore reported no unsaved
    changes after its PHI was stripped, so the next save skipped it and
    the identifier stayed in the database.

    `action: REMOVE` is the ordinary case: it is what the basic profile
    does and what `create_config` scaffolds.
    """
    session.configuration.phi_tags = {
        "0010,0010": {"name": "Patient Name", "action": "REMOVE"}}

    instance = session.store.patients[0].studies[0].series[0].instances[0]
    instance.set_attr("0010,0010", "John Doe")
    session.save(sync=True)
    assert not instance.has_unsaved_changes, "setup: the instance starts saved"

    session.anonymize()

    assert "0010,0010" not in instance.attributes, "setup: the tag was removed"
    assert instance.has_unsaved_changes, (
        "PHI was removed in memory but the instance was not marked for "
        "saving, so the next save skips it and the identifier survives")


def test_deleting_an_instance_removes_its_private_tag_values(session):
    """Private tag values live in their own table and must go with the row.

    `instance_attributes` holds private tags as text, keyed by instance
    UID. Its foreign key declares `ON DELETE CASCADE`, but SQLite
    enforces foreign keys only when `PRAGMA foreign_keys=ON`, which this
    store never sets -- so deleting the instance left its private tag
    values behind, readable and attributable by UID.
    """
    series = session.store.patients[0].studies[0].series[0]
    instance = series.instances[0]
    instance.set_attr("0009,0010", "PRIVATE IDENTIFIER")
    session.save(sync=True)

    with sqlite3.connect(session.store_backend.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM instance_attributes WHERE instance_uid=?",
            (instance.sop_instance_uid,)).fetchone()[0] == 1, (
                "setup: the private tag was stored")

    series.instances.remove(instance)
    session.save(sync=True)

    with sqlite3.connect(session.store_backend.db_path) as conn:
        remaining = conn.execute(
            "SELECT value_text FROM instance_attributes WHERE instance_uid=?",
            (instance.sop_instance_uid,)).fetchall()

    assert not remaining, (
        f"the deleted instance's private tag values survived: {remaining}")

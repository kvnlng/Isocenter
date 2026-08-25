"""Whether an item still carries identifiers, as recorded state.

Before this, the answer existed only as a set built inside `_export_dicom`
and discarded when it returned: to ask "which items in my session still
carry identifiers?" you had to re-run a scan, and the answer was never
attached to the items themselves.

`PhiStatus` records it. The whole design rests on one rule -- a status is
valid only for the revision it was computed at, so editing an item after
a scan returns it to UNSCANNED rather than leaving a stale claim behind.
A persisted REMEDIATED that survived a later edit would be worse than no
status at all, because it would read as an assurance.

Note what CLEARED does and does not mean: the configured tag scan found
nothing. It is not a statement about burned-in pixel text, which is a
separate scan (`scan_pixel_content`), and not an approval to release.
"""
import datetime
import sqlite3

import pytest

from isocenter.builders import DicomBuilder
from isocenter.entities import Patient, Study, Series, Instance, PhiStatus
from isocenter.session import DicomSession


@pytest.fixture
def session(tmp_path):
    """A session holding one patient with an obvious identifier."""
    sess = DicomSession(str(tmp_path / "session"))
    patient = (DicomBuilder.start_patient("P123", "John Doe")
               .add_study("S1", datetime.date(2023, 1, 1))
               .add_series("SE1", "CT", 1)
               .add_instance("I1", "1.2.3", 1)
               .end_instance().end_series().end_study().build())
    sess.store.patients.append(patient)
    yield sess
    sess.close()


@pytest.mark.parametrize("entity", [
    Patient("P1", "Test^Patient"),
    Study("S1", "20230101"),
    Series("SE1", "CT", 1),
    Instance("I1", "1.2.3"),
])
def test_nothing_is_assumed_about_an_unexamined_entity(entity):
    """The default is "not looked at", never "nothing found"."""
    assert entity.phi_status is PhiStatus.UNSCANNED


def test_a_scan_records_what_it_found_on_the_entities_themselves(session):
    """After an audit, an item can answer for itself."""
    session.audit()

    patient = session.store.patients[0]
    assert patient.phi_status is PhiStatus.IDENTIFIED


def test_an_edit_after_a_scan_invalidates_what_the_scan_concluded(session):
    """A status speaks for one revision, and says nothing about the next.

    This is the rule the whole feature rests on. Without it a stored
    status is an assurance about data that has since changed.
    """
    session.audit()
    patient = session.store.patients[0]
    assert patient.phi_status is not PhiStatus.UNSCANNED

    patient.mark_modified()

    assert patient.phi_status is PhiStatus.UNSCANNED


def test_remediating_an_entity_records_that_it_was_acted_on(session):
    """REMEDIATED is stamped after the change, so it survives its own edit.

    Remediation modifies the entity, which bumps the revision. Recording
    the status first would invalidate it immediately.
    """
    session.anonymize()

    patient = session.store.patients[0]
    assert patient.phi_status is PhiStatus.REMEDIATED


def test_a_status_survives_being_saved_and_loaded_back(session, tmp_path):
    """What the session learned is not thrown away when it closes."""
    session.audit()
    session.save(sync=True)
    db_path = session.store_backend.db_path
    session.close()

    reopened = DicomSession(db_path)
    try:
        assert reopened.store.patients[0].phi_status is PhiStatus.IDENTIFIED
    finally:
        reopened.close()


def test_a_stale_status_is_not_persisted_as_though_it_were_current(
        session, tmp_path):
    """An edit between the scan and the save must not be saved as scanned.

    The row written holds the edited attributes, so a status computed
    before the edit does not describe it. Persisting the stale value
    would make the reload trust a conclusion drawn about different data.
    """
    session.audit()
    patient = session.store.patients[0]
    patient.patient_name = "Someone Else"
    patient.mark_modified()

    session.save(sync=True)
    db_path = session.store_backend.db_path
    session.close()

    reopened = DicomSession(db_path)
    try:
        assert reopened.store.patients[0].phi_status is PhiStatus.UNSCANNED
    finally:
        reopened.close()


def test_a_database_created_before_the_column_existed_still_opens(tmp_path):
    """Upgrading must not require rebuilding the session.

    `CREATE TABLE IF NOT EXISTS` leaves an existing table untouched, so
    the column never appears in a store an earlier version created. The
    guarded ALTER adds it; rows predating it read as UNSCANNED, which is
    what they are.
    """
    db_path = str(tmp_path / "legacy.db")
    session = DicomSession(db_path)
    session.store.patients.append(
        Patient("P_LEGACY", "Older^Session"))
    session.save(sync=True)
    session.close()

    with sqlite3.connect(db_path) as conn:
        for table in ("patients", "studies", "instances"):
            conn.execute(f"ALTER TABLE {table} DROP COLUMN phi_status")

    reopened = DicomSession(db_path)
    try:
        assert reopened.store.patients[0].phi_status is PhiStatus.UNSCANNED
    finally:
        reopened.close()


def test_a_status_this_version_does_not_recognise_reads_as_unscanned(
        session, tmp_path):
    """An unrecognised claim is not an assurance.

    A value written by a future version, or a hand-edited row, must not
    be trusted just because it is present.
    """
    session.audit()
    session.save(sync=True)
    db_path = session.store_backend.db_path
    session.close()

    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE patients SET phi_status = 'definitely_fine'")

    reopened = DicomSession(db_path)
    try:
        assert reopened.store.patients[0].phi_status is PhiStatus.UNSCANNED
    finally:
        reopened.close()


def test_the_session_can_report_what_it_knows(session):
    """The point of recording state is being able to ask for it."""
    session.audit()

    summary = session.phi_status_summary()

    assert summary["patients"][PhiStatus.IDENTIFIED] == 1
    assert sum(summary["instances"].values()) == 1

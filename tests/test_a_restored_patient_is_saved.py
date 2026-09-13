"""A patient whose ID was put back is written, not deleted (#552).

`recover_patient_identity(restore=True)` assigned the original
`patient_name` and `patient_id` onto the `Patient` with no
`mark_modified()`, and `Patient` has no attribute tracking of its own. So
the next save found a clean patient, wrote no row under the restored ID,
found no row to hang the subtree on and skipped all of it -- and the
prune then deleted the pseudonym's row, and every study beneath it,
because no object in memory carried that ID any more. Measured on
1c41e5e: (1, 1, 1, 1) before the restore, (0, 0, 0, 0) after `save()`.

Two changes, and each has a test only it can fail:

- **The restore records the change** (`mark_modified()` on the patient).
  `test_a_name_only_restore_survives_a_reload_and_does_not_read_remediated`
  is its pin: a name-only lock restores the name under an ID whose row
  already exists, so the save's missing-row rule below cannot cover it.
- **The save writes a patient whose row does not exist**, whatever its
  bookkeeping says. `test_a_patient_renamed_without_mark_modified_is_still_written`
  is its pin: user code assigning `patient_id` reaches the same hole with
  no restore in sight.

`test_restore_then_save_keeps_the_patient` is the end-to-end regression;
either change alone keeps it green, which is why the two above exist.
"""
import sqlite3

import pytest

from isocenter import Session
from isocenter.entities import Instance, Patient, PhiStatus, Series, Study
from isocenter.persistence import SqliteStore

from support.ct_small_files import row_counts, write_ct

PID = "PAT-001"
NAME = "Test^PAT-001"


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def test_a_patient_renamed_without_mark_modified_is_still_written(tmp_path):
    """A clean patient with no row is, by definition, not persisted.

    No revision moves here and no setter is introduced: the save asks the
    store whether the row exists before it trusts `has_unsaved_changes`.
    """
    store = SqliteStore(str(tmp_path / "rename.db"))
    p = Patient("OLD", "Name^Old")
    st = Study("S", "20230101")
    se = Series("S.1", "CT", 1)
    se.instances.append(Instance("S.1.1", "1.2.3", 1, file_path="/tmp/s.dcm"))
    st.series.append(se)
    p.studies.append(st)
    store.save_all([p], prune_absent_patients=True)
    p.mark_subtree_persisted()

    p.patient_id = "NEW"
    assert not p.has_unsaved_changes
    store.save_all([p], prune_absent_patients=True)

    assert row_counts(store.db_path) == (1, 1, 1, 1)
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT patient_id FROM patients").fetchall() == [
            ("NEW",)]


def _first_session(tmp_path, tags_to_lock=None):
    """Ingest PAT-001, lock its identity, anonymize, save. Returns (db, key)."""
    write_ct(tmp_path / "first" / "a.dcm", PID, "1")
    db = str(tmp_path / "store.db")
    key = str(tmp_path / "isocenter.key")
    with Session(db) as session:
        session.ingest(str(tmp_path / "first"))
        session.enable_reversible_anonymization(key)
        report = session.audit()
        if tags_to_lock is None:
            session.lock_identities(PID)
        else:
            session.lock_identities(PID, tags_to_lock=tags_to_lock)
        session.anonymize(report)
        session.save(sync=True)
        pseudonym = session.store.patients[0].patient_id
    assert pseudonym.startswith("ANON_")
    assert row_counts(db) == (1, 1, 1, 1)
    return db, key, pseudonym


def test_restore_then_save_keeps_the_patient(tmp_path):
    db, key, pseudonym = _first_session(tmp_path)

    with Session(db) as session:
        session.enable_reversible_anonymization(key)
        session.recover_patient_identity(pseudonym, restore=True)
        assert session.store.patients[0].patient_id == PID
        session.save(sync=True)

    assert row_counts(db) == (1, 1, 1, 1)
    with Session(db) as session:
        [patient] = session.store.patients
        assert patient.patient_id == PID
        assert len(patient.studies) == 1


def test_a_name_only_restore_survives_a_reload_and_does_not_read_remediated(
        tmp_path):
    """The restore's own `mark_modified()`, pinned where nothing else covers it.

    Only (0010,0010) is locked, so the restore puts the name back under
    the pseudonym the patient already has a row for. The save's
    missing-row rule does not fire; before #552 the patient stayed clean,
    its row kept the replacement name, and it read a stale `REMEDIATED`
    over its original name -- in memory and after a reload.
    """
    db, key, pseudonym = _first_session(tmp_path, tags_to_lock=["0010,0010"])

    with Session(db) as session:
        session.enable_reversible_anonymization(key)
        session.recover_patient_identity(pseudonym, restore=True)
        patient = session.store.patients[0]
        assert patient.patient_name == NAME
        assert patient.patient_id == pseudonym
        status = patient.phi_status
        assert status is not PhiStatus.REMEDIATED
        session.save(sync=True)

    with Session(db) as session:
        [patient] = session.store.patients
        assert patient.patient_name == NAME
        status = patient.phi_status
        assert status is not PhiStatus.REMEDIATED

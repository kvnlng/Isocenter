"""A save never deletes a row that an object in memory still holds (#548).

`save_all` notices a removal by comparing, one parent at a time, the
rows stored under that parent's primary key with the children that
parent's list holds, and deletes the difference. That is correct only
while one row has one parent object. Two `Patient` objects carrying one
`patient_id` resolve to one `patients` row, and each object's scoped
delete removed the study the *other* object held: re-ingesting a new
study for an already-anonymized patient and then running `anonymize()`
made exactly that shape, and the next `save()`, `export()` or
`compact()` left 2 / 1 / 1 / 1 rows where there had been 3 / 3 / 3 / 3.

The rule now is the invariant the scoped delete was protecting all
along: a study, series or instance row is removed only when **no**
object in the saved list holds its UID. The held sets are built inside
the transaction, immediately before the deferred deletes, so a UID an
instance was renamed to mid-save is the one that counts --
`test_save_all_contract.py::test_an_instance_renamed_after_the_prepass_is_still_written`
is the pin for that placement.

Every graph here is built by hand and saved with `SqliteStore.save_all`
directly, with no `anonymize()`. The session-level merge (`#548`'s other
layer, `tests/test_patients_sharing_an_id_are_merged.py`) removes the
duplicate before the save ever sees it, so a session-level test would
stay green with this guard deleted; only a hand graph that reaches the
save still carrying the duplicate can see the guard at all.
"""
import logging
import sqlite3

import pytest

from isocenter.entities import Instance, Patient, Series, Study
from isocenter.persistence import SqliteStore

PID = "X-548"
NAME = "Doe^Jane"


@pytest.fixture
def store(tmp_path):
    return SqliteStore(str(tmp_path / "held.db"))


def _study(uid, series_uid=None, instance_uid=None):
    series_uid = series_uid or f"{uid}.1"
    instance_uid = instance_uid or f"{series_uid}.1"
    st = Study(uid, "20230101")
    se = Series(series_uid, "CT", 1)
    se.instances.append(Instance(instance_uid, "1.2.840.10008.5.1.4.1.1.2", 1,
                                 file_path=f"/tmp/{instance_uid}.dcm"))
    st.series.append(se)
    return st


def _rows(store, sql, args=()):
    with sqlite3.connect(store.db_path) as conn:
        return conn.execute(sql, args).fetchall()


def _counts(store):
    return tuple(_rows(store, f"SELECT COUNT(*) FROM {t}")[0][0]
                 for t in ("patients", "studies", "series", "instances"))


def _uids(store, table, column):
    return {r[0] for r in _rows(store, f"SELECT {column} FROM {table}")}


def test_two_patient_objects_with_one_id_keep_both_studies(store):
    """#548's shape, with nothing but the save in play.

    Measured on 1c41e5e before the guard: (1, 0, 0, 0) -- each object's
    scoped delete removed the other's study, and the reload was a patient
    with no studies.
    """
    stored = Patient(PID, NAME)
    stored.studies.append(_study("S1"))
    store.save_all([stored], prune_absent_patients=True)
    # As a reload leaves it: every node clean.
    stored.mark_subtree_persisted()

    arrived = Patient(PID, NAME)
    arrived.studies.append(_study("S3"))
    store.save_all([stored, arrived], prune_absent_patients=True)

    assert _counts(store) == (1, 2, 2, 2)
    assert _uids(store, "studies", "study_instance_uid") == {"S1", "S3"}

    reloaded = store.load_all()
    assert len(reloaded) == 1
    assert sorted(s.study_instance_uid for s in reloaded[0].studies) == [
        "S1", "S3"]


@pytest.mark.parametrize("level", ["study", "series"])
def test_two_parent_objects_sharing_a_row_keep_each_others_children(
        store, level):
    """The same shape one and two levels down.

    Two `Study` objects with one UID, each holding a different series; and
    two `Series` objects with one UID, each holding a different instance.
    A guard applied at the study level only would lose the children here.
    """
    patient = Patient(PID, NAME)
    if level == "study":
        a = _study("S1", series_uid="SE-A")
        b = _study("S1", series_uid="SE-B")
        patient.studies.extend([a, b])
        expected = ("series", "series_instance_uid", {"SE-A", "SE-B"})
    else:
        st = Study("S1", "20230101")
        for inst_uid in ("I-A", "I-B"):
            se = Series("SE1", "CT", 1)
            se.instances.append(Instance(
                inst_uid, "1.2.840.10008.5.1.4.1.1.2", 1,
                file_path=f"/tmp/{inst_uid}.dcm"))
            st.series.append(se)
        patient.studies.append(st)
        expected = ("instances", "sop_instance_uid", {"I-A", "I-B"})

    store.save_all([patient], prune_absent_patients=True)
    patient.mark_subtree_persisted()
    store.save_all([patient], prune_absent_patients=True)

    table, column, uids = expected
    assert _uids(store, table, column) == uids


def test_a_genuinely_removed_study_is_still_deleted(store):
    """The guard is not a licence to leak: a study nobody holds is removed.

    A guard that deleted nothing would keep every test above green, and
    would leave a removed study's identifiers in the store.
    """
    patient = Patient(PID, NAME)
    s1, s2 = _study("S1"), _study("S2")
    patient.studies.extend([s1, s2])
    store.save_all([patient], prune_absent_patients=True)
    patient.mark_subtree_persisted()

    patient.studies.remove(s1)
    store.save_all([patient], prune_absent_patients=True)

    assert _uids(store, "studies", "study_instance_uid") == {"S2"}
    assert _uids(store, "instances", "sop_instance_uid") == {"S2.1.1"}


def test_a_duplicate_reaching_the_save_is_logged_without_ids(store, caplog):
    """One WARNING, counts only: 0.9.7 took identifiers out of the log file.

    The store keeps one row per Patient ID, so the row's name and status
    come from whichever object the walk visited last. A caller who built
    the duplicate should hear that; the log is shipped beside exports, so
    it must not name the ID that collided.
    """
    first = Patient(PID, NAME)
    first.studies.append(_study("S1"))
    second = Patient(PID, NAME)
    second.studies.append(_study("S3"))

    with caplog.at_level(logging.DEBUG, logger="isocenter"):
        store.save_all([first, second], prune_absent_patients=True)

    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING and "Patient ID" in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in caplog.records]
    assert "2 Patient objects" in warnings[0].getMessage()
    for record in caplog.records:
        assert PID not in record.getMessage()
        assert NAME not in record.getMessage()


def test_no_duplicate_no_warning(store, caplog):
    """The warning is about a duplicate, not about every save."""
    patient = Patient(PID, NAME)
    patient.studies.append(_study("S1"))
    with caplog.at_level(logging.DEBUG, logger="isocenter"):
        store.save_all([patient, Patient("OTHER", NAME)],
                       prune_absent_patients=True)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

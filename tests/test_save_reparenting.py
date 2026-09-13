"""Moving a child between parents inside one save (#77).

`save_all` deletes rows the in-memory graph no longer contains, scoped
one parent at a time. A cross-parent move trips that: the old parent's
deletion pass can run before the new parent has adopted the child, so the
subtree is deleted and only partially re-inserted -- grandchildren are
written only when they report unsaved changes, and an untouched one does
not. The object stays intact in memory; the row does not. Nothing
reports it, and it surfaces on the next reload.
"""
import sqlite3

import pytest

from isocenter.entities import Patient, Study, Series, Instance
from isocenter.persistence import SqliteStore


@pytest.fixture
def store(tmp_path):
    return SqliteStore(str(tmp_path / "reparent.db"))


def _two_study_patient():
    """P1 with studies A and B; series SE1 (holding I1, I2) under A."""
    p = Patient("P1", "Patient One")
    a, b = Study("A", "20230101"), Study("B", "20230102")
    se = Series("SE1", "CT", 1)
    se.instances.extend([
        Instance("I1", "1.2.3", 1, file_path="/tmp/i1.dcm"),
        Instance("I2", "1.2.3", 2, file_path="/tmp/i2.dcm"),
    ])
    a.series.append(se)
    p.studies.extend([a, b])
    return p, a, b, se


def _rows(store, sql, args=()):
    with sqlite3.connect(store.db_path) as conn:
        return conn.execute(sql, args).fetchall()


def test_a_series_moved_between_studies_keeps_its_instances(store):
    """The issue's scenario: move SE1 from A to B, then save once."""
    p, a, b, se = _two_study_patient()
    store.save_all([p])

    a.series.remove(se)
    b.series.append(se)
    store.save_all([p])

    uids = {r[0] for r in _rows(
        store, "SELECT sop_instance_uid FROM instances")}
    assert uids == {"I1", "I2"}


def test_a_series_moved_between_studies_is_reparented(store):
    """Re-parenting mutates the study's list, which marks nothing dirty.

    So the series' own row is not rewritten by the ordinary upsert path
    and its study_id_fk would otherwise still point at the old study.
    """
    p, a, b, se = _two_study_patient()
    store.save_all([p])

    a.series.remove(se)
    b.series.append(se)
    store.save_all([p])

    rows = _rows(store, """
        SELECT st.study_instance_uid FROM series s
        JOIN studies st ON st.id = s.study_id_fk
        WHERE s.series_instance_uid = 'SE1'""")
    assert [r[0] for r in rows] == ["B"]


def test_the_move_survives_a_reload(store):
    """Memory was always right; the point is that the database now agrees."""
    p, a, b, se = _two_study_patient()
    store.save_all([p])

    a.series.remove(se)
    b.series.append(se)
    store.save_all([p])

    loaded = store.load_all()
    studies = {s.study_instance_uid: s for s in loaded[0].studies}
    assert [s.series_instance_uid for s in studies["A"].series] == []
    assert [s.series_instance_uid for s in studies["B"].series] == ["SE1"]
    assert len(studies["B"].series[0].instances) == 2


def test_an_instance_moved_between_series_survives_one_save(store):
    """The same shape one level down, which is where it started."""
    p = Patient("P2", "Patient Two")
    st = Study("S1", "20230101")
    se1, se2 = Series("SE1", "CT", 1), Series("SE2", "CT", 2)
    inst = Instance("I1", "1.2.3", 1, file_path="/tmp/i1.dcm")
    se1.instances.append(inst)
    st.series.extend([se1, se2])
    p.studies.append(st)
    store.save_all([p])

    se1.instances.remove(inst)
    se2.instances.append(inst)
    store.save_all([p])

    rows = _rows(store, """
        SELECT s.series_instance_uid FROM instances i
        JOIN series s ON s.id = i.series_id_fk
        WHERE i.sop_instance_uid = 'I1'""")
    assert [r[0] for r in rows] == ["SE2"]


def test_a_genuinely_removed_series_is_still_deleted(store):
    """The deletion diff must keep working; this is not a licence to leak."""
    p, a, b, se = _two_study_patient()
    store.save_all([p])

    a.series.remove(se)
    store.save_all([p])

    assert _rows(store, "SELECT 1 FROM series WHERE series_instance_uid='SE1'") == []
    assert _rows(store, "SELECT 1 FROM instances") == []


# ---------------------------------------------------------------------------
# One level up: a study follows the patient that holds it (#551).
#
# `_reparent_series` and `_reparent_instances` were added for #77, and the
# study level had no sibling. A study the pass left unchanged is not
# upserted, so its `patient_id_fk` went on naming the patient's OLD row
# after the patient's ID was replaced -- and `_delete_absent_patients`
# then deleted that row together with the study beneath it. A dated study
# escaped only because SHIFT_DATE had dirtied it.
# ---------------------------------------------------------------------------

def _patient_with_one_study(pid="OLD"):
    p = Patient(pid, "Patient Old")
    st = Study("S", "20230101")
    se = Series("S.1", "CT", 1)
    se.instances.append(Instance("S.1.1", "1.2.3", 1, file_path="/tmp/s.dcm"))
    st.series.append(se)
    p.studies.append(st)
    return p, st


def _counts(store):
    return tuple(_rows(store, f"SELECT COUNT(*) FROM {t}")[0][0]
                 for t in ("patients", "studies", "series", "instances"))


def test_a_clean_study_moved_between_patients_is_reparented(store):
    """A list mutation marks nothing, so the save corrects the key itself."""
    p1, st = _patient_with_one_study("P1")
    p2 = Patient("P2", "Patient Two")
    store.save_all([p1, p2], prune_absent_patients=True)
    p1.mark_subtree_persisted()
    p2.mark_subtree_persisted()

    p1.studies.remove(st)
    p2.studies.append(st)
    store.save_all([p1, p2], prune_absent_patients=True)

    rows = _rows(store, """
        SELECT p.patient_id FROM studies st
        JOIN patients p ON p.id = st.patient_id_fk
        WHERE st.study_instance_uid = 'S'""")
    assert [r[0] for r in rows] == ["P2"]
    loaded = {p.patient_id: p for p in store.load_all()}
    assert [s.study_instance_uid for s in loaded["P2"].studies] == ["S"]
    assert len(loaded["P2"].studies[0].series[0].instances) == 1
    assert loaded["P1"].studies == []


def test_a_renamed_patient_keeps_its_clean_study(store):
    """#551 with nothing but the save: a new ID, and a study nobody touched.

    Measured on 1c41e5e: (1, 0, 0, 0). The new ID's row was inserted, the
    study row still named the old one, and the prune took it.
    """
    p, st = _patient_with_one_study("OLD")
    store.save_all([p], prune_absent_patients=True)
    p.mark_subtree_persisted()

    p.patient_id = "NEW"
    p.mark_modified()
    assert not st.has_unsaved_changes
    store.save_all([p], prune_absent_patients=True)

    assert _counts(store) == (1, 1, 1, 1)
    assert _rows(store, "SELECT patient_id FROM patients") == [("NEW",)]


#: Both parallel paths; `audit()` clones the graph whichever it picks.
MODES = ["threads", "processes"]


@pytest.fixture
def mode(request, monkeypatch):
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)
    if request.param == "threads":
        monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
        monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    else:
        monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")
        monkeypatch.delenv("ISOCENTER_FORCE_THREADS", raising=False)
    return request.param


@pytest.mark.parametrize("mode", MODES, indirect=True)
def test_anonymize_after_a_reload_keeps_an_undated_study(tmp_path, mode):
    """#551 end to end, through the public API only.

    One patient whose study has no StudyDate, so no finding dirties the
    study: `ingest()`, `audit()`, `save()`; reopen; `audit()` records the
    status the study already carries (the #173 short-circuit) and
    `anonymize()` replaces the Patient ID. Measured on 1c41e5e:
    (1, 1, 1, 1) after the first session and (1, 0, 0, 0) after the second.
    """
    from isocenter import Session
    from support.ct_small_files import row_counts, write_ct

    write_ct(tmp_path / "in" / "a.dcm", "PAT-001", "1", study_date=None)
    db = tmp_path / "store.db"
    with Session(str(db)) as session:
        session.ingest(str(tmp_path / "in"))
        session.audit()
        session.save(sync=True)
    assert row_counts(db) == (1, 1, 1, 1)

    with Session(str(db)) as session:
        report = session.audit()
        study = session.store.patients[0].studies[0]
        session.anonymize(report)
        assert session.store.patients[0].patient_id.startswith("ANON_")
        assert not study.has_unsaved_changes, (
            "the study was dirtied by the pass, so this no longer reaches "
            "the clean-study path it exists to cover")
        session.save(sync=True)

    assert row_counts(db) == (1, 1, 1, 1)

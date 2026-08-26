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

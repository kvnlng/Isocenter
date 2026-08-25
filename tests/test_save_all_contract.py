"""What `save_all()` guarantees, pinned before the function was split up.

`SqliteStore.save_all` was a single 336-line method walking the whole
patient graph, so the behaviour it promised was only discoverable by
reading it end to end. These tests state the promises directly: what
lands in which table, what a second save is allowed to rewrite, and --
the two that mattered most -- what happens when the save fails.

Failure is the interesting case because `save_all` is the last thing
standing between an in-memory de-identified graph and the disk. A save
that fails while claiming to have succeeded is indistinguishable from
one that worked, until the data is gone.
"""
import os
import sqlite3

import numpy as np
import pytest

from isocenter.entities import Patient, Study, Series, Instance, Equipment
from isocenter.persistence import SqliteStore


@pytest.fixture
def store(tmp_path):
    """A file-backed store. Not `:memory:` -- the two share a code path
    only up to `_get_connection`, and the file branch is the one that
    closes its connection on the way out."""
    s = SqliteStore(str(tmp_path / "contract.db"))
    yield s


def make_patient(pid="P1", n_instances=1, with_pixels=False):
    patient = Patient(pid, "Test^Patient")
    study = Study(f"{pid}.STUDY", "20230101")
    series = Series(f"{pid}.SERIES", "CT", 1)
    series.equipment = Equipment("GE", "Revolution CT", "SN-1")
    for i in range(n_instances):
        inst = Instance(f"{pid}.INST.{i}", "/tmp/x.dcm")
        inst.sop_class_uid = "1.2.840.10008.5.1.4.1.1.2"
        inst.instance_number = i + 1
        inst.set_attr("0010,0020", pid)
        if with_pixels:
            inst.set_pixel_data(np.full((8, 8), i + 1, dtype=np.uint8))
        series.instances.append(inst)
    study.series.append(series)
    patient.studies.append(study)
    return patient


# --------------------------------------------------------------------
# Failure behaviour
# --------------------------------------------------------------------

def test_a_failed_save_reports_the_error_that_caused_it(store, monkeypatch):
    """The exception that escapes is the real one, not a casualty of cleanup.

    `_get_connection` already rolls back and closes on the way out, so
    `save_all`'s own error handler used to call `rollback()` on a
    connection that was closed a moment earlier. That call raises
    `ProgrammingError: Cannot operate on a closed database`, which then
    replaces the original exception as the one the caller sees -- every
    distinct save failure arriving under the same misleading name.
    """
    def explode(*_args, **_kwargs):
        raise ValueError("serialization blew up")

    monkeypatch.setattr(store, "_serialize_item", explode)

    with pytest.raises(ValueError, match="serialization blew up"):
        store.save_all([make_patient()])


def test_a_save_that_never_opened_a_connection_still_reports_its_error(
        tmp_path, monkeypatch):
    """A failure before the connection exists must not become a NameError.

    The old handler tested `hasattr(conn, "rollback")`, but `conn` is
    bound by the `with` statement it guards. If opening the database was
    itself what failed, the name was never bound and the handler died
    with `UnboundLocalError`, discarding the real reason.
    """
    store = SqliteStore(str(tmp_path / "unopenable.db"))

    def cannot_connect(*_args, **_kwargs):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(store, "_get_connection", cannot_connect)

    with pytest.raises(sqlite3.OperationalError, match="unable to open"):
        store.save_all([make_patient()])


def test_instances_stay_unsaved_when_the_save_fails(store, monkeypatch):
    """A rolled-back save must not leave memory believing it was written.

    Instances were marked clean inside the loop, one series at a time,
    while the transaction covering all of them was still open. A failure
    on any later patient rolled the whole transaction back -- so the rows
    were gone from the database, but the already-processed objects no
    longer considered themselves dirty and the *next* save skipped them.
    One failed write, silently permanent.
    """
    saved_first, fails_second = make_patient("P_OK"), make_patient("P_BAD")
    survivors = saved_first.studies[0].series[0].instances
    assert all(i.has_unsaved_changes for i in survivors)

    real_serialize = store._serialize_item

    def explode_on_the_second_patient(inst):
        if inst.sop_instance_uid.startswith("P_BAD"):
            raise ValueError("disk I/O error")
        return real_serialize(inst)

    monkeypatch.setattr(store, "_serialize_item", explode_on_the_second_patient)

    with pytest.raises(ValueError):
        store.save_all([saved_first, fails_second])

    with sqlite3.connect(store.db_path) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM instances").fetchone()[0]
    assert rows == 0, "sanity check: the transaction did roll back"

    assert all(i.has_unsaved_changes for i in survivors), (
        "the failed save marked instances persisted, so a retry would "
        "skip them")


# --------------------------------------------------------------------
# What lands where
# --------------------------------------------------------------------

def test_private_tags_are_split_out_into_the_vertical_table(store):
    """Odd-group tags go to `instance_attributes`; standard tags stay inline."""
    patient = make_patient()
    inst = patient.studies[0].series[0].instances[0]
    inst.set_attr("0009,0010", "PRIVATE CREATOR")
    inst.set_attr("0008,0060", "CT")

    store.save_all([patient])

    with sqlite3.connect(store.db_path) as conn:
        vertical = conn.execute(
            "SELECT group_id, element_id, value_text FROM instance_attributes "
            "WHERE instance_uid=?", (inst.sop_instance_uid,)).fetchall()
        core = conn.execute(
            "SELECT attributes_json FROM instances WHERE sop_instance_uid=?",
            (inst.sop_instance_uid,)).fetchone()[0]

    assert ("0009", "0010", "PRIVATE CREATOR") in vertical
    assert "0008,0060" in core
    assert "0009,0010" not in core


def test_unchanged_pixels_are_not_written_to_the_sidecar_twice(store):
    """The second save of an untouched frame reuses the first one's bytes."""
    patient = make_patient(n_instances=2, with_pixels=True)
    store.save_all([patient])

    first_size = os.path.getsize(store.sidecar.filepath)

    for inst in patient.studies[0].series[0].instances:
        inst.mark_modified()        # modified, but the pixels are identical
    store.save_all([patient])

    with sqlite3.connect(store.db_path) as conn:
        offsets = conn.execute(
            "SELECT pixel_offset FROM instances ORDER BY sop_instance_uid"
        ).fetchall()

    assert all(o[0] is not None for o in offsets)
    assert os.path.getsize(store.sidecar.filepath) == first_size, (
        "identical pixel data was appended to the sidecar a second time")


def test_instance_blobs_mirrors_every_stored_frame(store):
    """Compaction reads `instance_blobs`; a frame missing there is lost."""
    patient = make_patient(n_instances=2, with_pixels=True)
    store.save_all([patient])

    with sqlite3.connect(store.db_path) as conn:
        rows = conn.execute(
            "SELECT i.sop_instance_uid, i.pixel_offset, b.offset "
            "FROM instances i LEFT JOIN instance_blobs b "
            "  ON b.instance_uid = i.sop_instance_uid AND b.kind = 'pixels'"
        ).fetchall()

    assert rows
    for uid, instance_offset, blob_offset in rows:
        assert blob_offset == instance_offset, (
            f"instance_blobs disagrees with instances for {uid}")


def test_instances_removed_from_memory_are_deleted_from_the_database(store):
    """The saved graph is the in-memory graph, not a union with the old one."""
    patient = make_patient(n_instances=3)
    store.save_all([patient])

    series = patient.studies[0].series[0]
    removed = series.instances.pop(1)
    store.save_all([patient])

    with sqlite3.connect(store.db_path) as conn:
        remaining = {r[0] for r in conn.execute(
            "SELECT sop_instance_uid FROM instances").fetchall()}

    assert removed.sop_instance_uid not in remaining
    assert len(remaining) == 2

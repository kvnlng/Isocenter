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
import contextlib
import datetime
import os
import sqlite3
from threading import Event

import numpy as np
import pytest

from isocenter.builders import DicomBuilder
from isocenter.entities import (Patient, Study, Series, Instance, Equipment,
                                TrackedEntity)
from isocenter.persistence import SqliteStore
from isocenter.session import DicomSession


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


def test_no_sidecar_frame_is_written_while_the_database_transaction_is_open(
        store, monkeypatch):
    """Bulk pixel I/O must not run under the SQLite write lock.

    `save_all` opened its transaction and then, deep inside the walk,
    appended every dirty instance's pixel frame to the sidecar -- so the
    write lock was held for as long as it took to compress and write the
    save's whole resident pixel payload. On slow storage, or with a large
    enough dirty set, that window can outlast `_SQLITE_BUSY_TIMEOUT_S`
    and turn a perfectly healthy writer in another thread or process into
    a spurious `sqlite3.OperationalError: database is locked` -- the #250
    shape, arriving from a save that was merely slow.

    This is structural, not a timing test. Two seams are instrumented:
    `_get_connection` carries a DEPTH counter (a counter, not a flag --
    `record_blob_ref` and `save_vertical_attributes` are handed the open
    connection, and `_init_db` opened one at construction), and
    `sidecar.write_frame` records the depth it sees. Every recorded depth
    must be 0. Before the restructure the list read `[1, 1, 1, 1]`.

    Two patients of two instances each, and the count is asserted
    EXACTLY: a partial restructure that hoisted only the first series
    would otherwise be caught only by the depths it left behind, and if
    the fixture's frames ever started deduplicating, a partial hoist
    could write its remaining frames at depth 0 by accident and pass. The
    exact count is also what stops a fixture regression making the depth
    assertion vacuously true.
    """
    depth = {"n": 0}
    depths_at_write = []

    original_get_connection = store._get_connection

    @contextlib.contextmanager
    def counting_connection(*args, **kwargs):
        depth["n"] += 1
        try:
            with original_get_connection(*args, **kwargs) as conn:
                yield conn
        finally:
            depth["n"] -= 1

    monkeypatch.setattr(store, "_get_connection", counting_connection)

    original_write_frame = store.sidecar.write_frame

    def recording_write_frame(*args, **kwargs):
        depths_at_write.append(depth["n"])
        return original_write_frame(*args, **kwargs)

    monkeypatch.setattr(store.sidecar, "write_frame", recording_write_frame)

    store.save_all([make_patient("PA", n_instances=2, with_pixels=True),
                    make_patient("PB", n_instances=2, with_pixels=True)])

    assert len(depths_at_write) == 4, (
        f"expected one frame per instance across both patients, got "
        f"{len(depths_at_write)}. Either the fixture stopped carrying "
        "distinct pixels (making the depth assertion vacuous) or only "
        "some of the walk was hoisted into the prepass.")
    assert all(d == 0 for d in depths_at_write), (
        f"sidecar frames were appended with {max(depths_at_write)} database "
        f"transaction(s) open (depths {depths_at_write}). The SQLite write "
        "lock is then held for as long as the save's dirty resident pixel "
        "bytes take to compress and write, so a slow-storage save can "
        "exceed _SQLITE_BUSY_TIMEOUT_S and surface in another writer as a "
        "spurious 'database is locked' (#287).")


def test_an_instance_changed_after_the_prepass_keeps_its_stored_frame(
        store, monkeypatch):
    """The #274 guard, re-checked at the last moment before the row is emitted.

    #287 moved the sidecar appends into a prepass that runs before the
    transaction opens, which stretched the capture -> commit window to
    the length of all of the save's pixel I/O. A redaction landing inside
    that window rebinds `_pixel_loader` to the redacted frame while the
    prepared map still holds the pristine one. Emitting the prepared
    frame would leave `instance_blobs` naming a frame `instances` no
    longer describes, and the next `compact_sidecar` -- which reads
    `instance_blobs` -- would copy the stale frame forward and discard
    the current one: pre-redaction pixels resurrected, silently.

    So `_build_instance_writes` re-evaluates `inst._revision !=
    revision` immediately before emitting, and on a trip substitutes
    `_StoredFrame(None, None, None, None)` and skips the `instance_blobs`
    entry. The seam used here is `_serialize_item`, which runs at the top
    of the same loop iteration -- inside the transaction, after the
    prepass -- so moving `_revision` from it lands exactly in the window.

    Four assertions, one per half of the claim, because "no exception"
    would pass with the re-check deleted:

    1. The instance's row is still written, NOT dropped -- asserted via
       a core attribute edited before this save and read back out of
       `attributes_json`. Asserting merely that the row EXISTS would be
       vacuous: it already exists from the first save, so its presence
       cannot distinguish "written with an all-None frame" from "not
       written at all". Dropping the row instead of substituting the
       frame passes the whole rest of the suite; this assertion is the
       only thing that catches it. That distinction matters because
       writing the row is what a raced instance got before the
       restructure, and #287 is a restructure.
    2. `instances.pixel_offset` is unchanged -- the all-None frame plus
       the upsert's COALESCE.
    3. `instance_blobs` is unchanged -- the skipped `blob_rows` entry.
       This is the half compaction reads.
    4. The instance is still dirty, because `mark_persisted` got the
       stale capture, so the next save writes the truth.

    The "a new frame really was appended" assertion is the guard against
    a vacuous pass: without it, a prepass that quietly stopped writing
    frames would satisfy 2 and 3 for the wrong reason.
    """
    patient = make_patient(n_instances=1, with_pixels=True)
    inst = patient.studies[0].series[0].instances[0]
    store.save_all([patient])

    def stored_attributes():
        with sqlite3.connect(store.db_path) as conn:
            row = conn.execute(
                "SELECT attributes_json FROM instances "
                "WHERE sop_instance_uid=?", (inst.sop_instance_uid,)
            ).fetchone()
        return None if row is None else row[0]

    def stored_frame():
        with sqlite3.connect(store.db_path) as conn:
            row = conn.execute(
                "SELECT pixel_offset, pixel_length FROM instances "
                "WHERE sop_instance_uid=?", (inst.sop_instance_uid,)
            ).fetchone()
            blob = conn.execute(
                "SELECT offset, length FROM instance_blobs "
                "WHERE instance_uid=? AND kind='pixels'",
                (inst.sop_instance_uid,)).fetchone()
        return row, blob

    before = stored_frame()
    assert before[0][0] is not None and before[1] is not None

    sidecar_size_before = os.path.getsize(store.sidecar.filepath)

    # Different bytes, so the prepass must append a genuinely new frame
    # rather than take the dedup branch.
    inst.set_pixel_data(np.full((8, 8), 200, dtype=np.uint8))

    # A core attribute edit, so "the row was written" is an assertion
    # with teeth rather than a restatement of "the row exists".
    inst.set_attr("0008,103e", "edited before the losing save")
    assert "edited before the losing save" not in (stored_attributes() or "")

    real_serialize = store._serialize_item
    raced = []

    def race_the_instance(item):
        serialized = real_serialize(item)
        if item is inst and not raced:
            raced.append(True)
            # A mutation landing between the prepass's capture and this
            # row being built -- what a concurrent redaction does.
            item.mark_modified()
        return serialized

    monkeypatch.setattr(store, "_serialize_item", race_the_instance)

    store.save_all([patient])

    assert raced, "the race was never forced; _serialize_item was not reached"
    assert os.path.getsize(store.sidecar.filepath) > sidecar_size_before, (
        "the prepass appended no new frame, so this test would pass even "
        "with the re-check deleted")

    after = stored_frame()
    assert "edited before the losing save" in (stored_attributes() or ""), (
        "the raced instance's row was not written. The guard must "
        "substitute an all-None FRAME, not drop the row -- writing the "
        "row with the stored frame reference left alone is exactly what a "
        "raced instance got before the restructure, and dropping it is a "
        "semantic change beyond #287's remit")
    assert after[0] == before[0], (
        "instances.pixel_offset moved to the frame the prepass wrote, "
        "which the instance had already left by commit time (#274/#287)")
    assert after[1] == before[1], (
        "instance_blobs moved to the prepared frame. Compaction reads "
        "this table, so it would copy the stale frame forward and "
        "discard the current one")
    assert inst.has_unsaved_changes, (
        "the raced instance was marked clean, so the next save would "
        "never correct the row this one deliberately did not write")


def test_an_instance_renamed_after_the_prepass_is_still_written(
        store, monkeypatch):
    """A UID that moves mid-save must not cost the instance its row.

    Redaction mutates `sop_instance_uid` **in place** --
    `Instance.regenerate_uid()` and `Session._apply_redaction_outcomes`
    both do -- so the UID an instance had when the prepass looked at it
    is not necessarily the UID it has when the transaction walks it.

    Keying the prepared map by UID made that a data-loss bug rather than
    a missed optimisation, because the two halves of the save then
    disagree about which name is real. `_save_unsaved_instances` filtered
    on `sop_instance_uid in prepared`, so a renamed instance was skipped
    and never inserted -- while `_delete_removed_instances`, which
    computes `stored - {i.sop_instance_uid for i in series.instances}`,
    saw the OLD row orphaned and deleted it, cascading `instance_blobs`.
    The row was gone and no row replaced it.

    Not self-healing. The instance stays dirty, but nothing reconciles
    that: `Session.close()` shuts the persistence manager down without
    enqueuing a save, so a session closing after the losing save leaves
    the index permanently missing an instance whose source file is
    untouched -- index loss, not file loss -- with the prepass's frame
    orphaned and `_log_save_summary` still reporting the instance saved.

    Before the prepass existed the filter was `has_unsaved_changes`, so
    the entity itself was selected, inserted under its NEW UID, and the
    delete found nothing orphaned. That is the behaviour this pins, and
    keying the prepared map by object identity is what restores it:
    identity survives both a rename and a re-parenting, which is why it
    is the right key and the UID never was.

    The seam is `_get_connection` -- it opens after the prepass has
    finished and before the walk begins, which is exactly the window.
    """
    patient = make_patient(n_instances=1, with_pixels=True)
    inst = patient.studies[0].series[0].instances[0]
    original_uid = inst.sop_instance_uid
    store.save_all([patient])

    inst.set_attr("0008,103e", "dirtied before the losing save")

    original_get_connection = store._get_connection
    renamed = []

    @contextlib.contextmanager
    def rename_then_connect(*args, **kwargs):
        if not renamed:
            renamed.append(True)
            # In place, the way `regenerate_uid()` does it. Plain
            # attribute assignment, so `_revision` does not move and the
            # late #274 re-check is not what is under test here.
            inst.sop_instance_uid = "RENAMED.MID.SAVE"
        with original_get_connection(*args, **kwargs) as conn:
            yield conn

    monkeypatch.setattr(store, "_get_connection", rename_then_connect)

    store.save_all([patient])

    assert renamed, "the rename never fired, so no window was forced"

    with sqlite3.connect(store.db_path) as conn:
        rows = {r[0] for r in conn.execute(
            "SELECT sop_instance_uid FROM instances").fetchall()}

    assert rows == {"RENAMED.MID.SAVE"}, (
        f"expected the renamed instance's row and nothing else, got {rows}. "
        f"An empty set means the old row ({original_uid}) was deleted as "
        "orphaned while the renamed instance was skipped by the prepared "
        "map -- the instance is then absent from the index entirely, and "
        "nothing will put it back")
    assert not inst.has_unsaved_changes, (
        "the renamed instance was never written, so it stays dirty -- and "
        "close() does not enqueue a save, so that dirt is never resolved")


# --------------------------------------------------------------------
# The capture window, from the other side: audit() (#297)
# --------------------------------------------------------------------

def test_audit_drains_a_running_save_before_it_records_what_it_found(
        tmp_path, monkeypatch):
    """No status is stamped while a background save is mid-capture (#297).

    A scan ends by advancing `_revision` on every entity it touched --
    `_record_scan_results` calls `record_phi_status`, and a new status
    is a change the store should hold. `save()` without `sync=True`
    returns with `save_all` still running on the persistence manager's
    thread, and since #287 that window is as long as all of the save's
    pixel I/O. An instance dirtied inside that window is not in the
    dirty set the save froze, so the save neither writes it nor marks
    it persisted: it is simply left dirty, and nothing saves it
    afterwards, because `close()` shuts the manager down without
    enqueuing a save. The direction is safe -- the status is lost, not
    a false clean claim -- but it is lost silently.

    This is reachable on the *documented* order: README and
    `docs/quickstart.md` both show `session.save()` before
    `report = session.audit()`.

    Deterministic rather than timed. The save is parked inside
    `save_all` until something releases it, and the only thing that
    releases it is `audit()`'s own flush -- so a run that reaches
    `_record_scan_results` with the save still parked is a run where
    the flush is missing, not a run that lost a race. Both waits are
    bounded so a wedge fails instead of hanging.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")

    session = DicomSession(str(tmp_path / "session"))
    patient = (DicomBuilder.start_patient("P123", "John Doe")
               .add_study("S1", datetime.date(2023, 1, 1))
               .add_series("SE1", "CT", 1)
               .add_instance("I1", "1.2.3", 1)
               .end_instance().end_series().end_study().build())
    session.store.patients.append(patient)

    started, release, finished = Event(), Event(), Event()

    real_save_all = session.store_backend.save_all

    def parked_save_all(*args, **kwargs):
        started.set()
        release.wait(10)
        try:
            return real_save_all(*args, **kwargs)
        finally:
            finished.set()

    real_flush = session.persistence_manager.flush

    def releasing_flush():
        release.set()
        return real_flush()

    observations = []
    real_record = TrackedEntity.record_phi_status

    def watched_record(self, status):
        observations.append(started.is_set() and not finished.is_set())
        return real_record(self, status)

    monkeypatch.setattr(session.store_backend, "save_all", parked_save_all)
    monkeypatch.setattr(session.persistence_manager, "flush", releasing_flush)
    monkeypatch.setattr(TrackedEntity, "record_phi_status", watched_record)

    try:
        session.save()
        assert started.wait(5), (
            "the background save never entered save_all, so the window "
            "this test measures never opened")

        session.audit()

        assert observations, (
            "no status was recorded, so the assertion below is vacuous")
        assert not any(observations), (
            "a PHI status was stamped while save_all was mid-capture: the "
            "instance is dropped from the frozen dirty set, left dirty and "
            "never saved, its prepared frame discarded under COALESCE, and "
            "close() says nothing")
    finally:
        release.set()
        session.close()

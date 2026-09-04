"""
Persistence layer for Isocenter.

This module provides the SqliteStore class which manages the storage and retrieval
of DICOM entities (Patients, Studies, Series, Instances) using a SQLite database.
It also handles sidecar storage for pixel data to keep the database lightweight.
"""

import sqlite3
import contextlib
import os
import tempfile
import json
import queue
import threading
import time
import weakref
import hashlib
import base64
import traceback
from typing import List, Optional, Dict, Any, Tuple, NamedTuple
from dataclasses import dataclass
from datetime import date, datetime
from contextlib import nullcontext

from pydicom.multival import MultiValue

from .entities import (Patient, Study, Series, Instance, Equipment,
                       PhiStatus)
from .sidecar import SidecarManager
from .logger import get_logger
from .privacy import PhiFinding, PhiRemediation
from .io_handlers import SidecarPixelLoader



# zlib is the only algorithm `save_all` writes. Named so the value and the
# column it lands in cannot drift apart.
_PIXEL_COMPRESSION = 'zlib'

# How many SOP Instance UIDs go into one `instance_attributes` lookup.
# SQLite's default SQLITE_MAX_VARIABLE_NUMBER has been 999 on builds old
# enough to still be around, so a chunk has to stay well under it; the
# point of chunking is that hydrating 10k instances costs a score of
# queries rather than 10k of them.
_VERTICAL_UID_CHUNK = 500

_UPSERT_INSTANCE_SQL = """
    INSERT INTO instances (series_id_fk, sop_instance_uid, sop_class_uid, instance_number, file_path,
                           source_path,
                           pixel_offset, pixel_length, pixel_hash, compress_alg, attributes_json,
                           phi_status)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(sop_instance_uid) DO UPDATE SET
        series_id_fk=excluded.series_id_fk,
        sop_class_uid=excluded.sop_class_uid,
        instance_number=excluded.instance_number,
        file_path=excluded.file_path,
        source_path=COALESCE(excluded.source_path, instances.source_path),
        attributes_json=excluded.attributes_json,
        phi_status=excluded.phi_status,
        pixel_offset=COALESCE(excluded.pixel_offset, instances.pixel_offset),
        pixel_length=COALESCE(excluded.pixel_length, instances.pixel_length),
        pixel_hash=COALESCE(excluded.pixel_hash, instances.pixel_hash),
        compress_alg=COALESCE(excluded.compress_alg, instances.compress_alg)
"""


def _phi_status_from_stored(value) -> PhiStatus:
    """The status a stored row claims, defaulting to UNSCANNED.

    Rows written before the column existed, and any value this version
    does not recognise, read as UNSCANNED. An unrecognised claim must
    never present as an assurance.
    """
    try:
        return PhiStatus(value)
    except ValueError:
        return PhiStatus.UNSCANNED


def _in_clause(values):
    """A parameter placeholder list for an IN clause of this length."""
    return ",".join("?" * len(values))


def _delete_instances(cur, uids) -> None:
    """Deletes instances and everything keyed to them.

    `instance_attributes` and `instance_blobs` are keyed by instance UID.
    The first declares `ON DELETE CASCADE`, but SQLite enforces foreign
    keys only under `PRAGMA foreign_keys=ON`, which this store never sets
    -- so nothing cascades and the rows have to be removed explicitly.

    That matters more than tidiness: `instance_attributes` holds private
    tag *values* as text. Leaving them behind after deleting the instance
    leaves identifiable content in the database, still attributable by
    UID.
    """
    if not uids:
        return
    rows = [(uid,) for uid in uids]
    cur.executemany("DELETE FROM instance_attributes WHERE instance_uid=?", rows)
    cur.executemany("DELETE FROM instance_blobs WHERE instance_uid=?", rows)
    cur.executemany("DELETE FROM instances WHERE sop_instance_uid=?", rows)


def _delete_series_subtrees(cur, series_pks) -> None:
    """Deletes series rows and every instance beneath them."""
    if not series_pks:
        return
    clause = _in_clause(series_pks)
    uids = [row[0] for row in cur.execute(
        f"SELECT sop_instance_uid FROM instances WHERE series_id_fk IN ({clause})",
        series_pks).fetchall()]
    _delete_instances(cur, uids)
    cur.execute(f"DELETE FROM series WHERE id IN ({clause})", series_pks)


def _delete_study_subtrees(cur, study_pks) -> None:
    """Deletes study rows and every series and instance beneath them."""
    if not study_pks:
        return
    clause = _in_clause(study_pks)
    series_pks = [row[0] for row in cur.execute(
        f"SELECT id FROM series WHERE study_id_fk IN ({clause})",
        study_pks).fetchall()]
    _delete_series_subtrees(cur, series_pks)
    cur.execute(f"DELETE FROM studies WHERE id IN ({clause})", study_pks)


def _delete_patient_subtrees(cur, patient_pks) -> None:
    """Deletes patient rows and everything beneath them."""
    if not patient_pks:
        return
    clause = _in_clause(patient_pks)
    study_pks = [row[0] for row in cur.execute(
        f"SELECT id FROM studies WHERE patient_id_fk IN ({clause})",
        patient_pks).fetchall()]
    _delete_study_subtrees(cur, study_pks)
    cur.execute(f"DELETE FROM patients WHERE id IN ({clause})", patient_pks)


#: SQLite busy timeout for every file-backed connection, in seconds.
#: This number was 900.0, inline and unexplained, and #250 measured what
#: that buys: a writer that cannot get the lock in two minutes is not
#: going to get it at second 890 -- the stuck forked child errored at
#: exactly 900s every time -- and each hit became a ~15-minute stall
#: that CI's job cap killed as 'cancelled' with no failing test named.
#: The invariant (pinned by test_packaging_contract.py, with pytest's
#: faulthandler_timeout=300 and the Run Tests step cap): a lock that
#: will not clear surfaces as `sqlite3.OperationalError: database is
#: locked` *inside* one faulthandler window, where the dump shows a
#: thread still waiting with a stack -- never as a stall for an outer
#: timeout to kill. The VALUE has not moved, but its justification has
#: (#287). It used to rest on a measurement -- the longest single
#: transaction window across the stress pipeline (4,000 instances, ~2GB
#: of pixels; `save_all` compressing dirty frames into the sidecar
#: inside its connection window) was 1.6s, and 120 was ~75x that. Since
#: #287 hoisted the sidecar writes into a prepass, the window contains
#: no bulk I/O at all: row upserts only, bounded by row count rather
#: than by pixel bytes or storage throughput. That is a stronger
#: justification, not a reason to shrink the number -- and NOT a reason
#: to inline or delete the constant. The timeout is a diagnostic for a
#: STUCK writer (another process, a stale WAL), which is still possible;
#: `test_packaging_contract.py` asserts both that the value is under the
#: faulthandler window and, by `inspect.getsource`, that
#: `_get_connection` still READS this name, because a re-inlined literal
#: is exactly how 900.0 survived unquestioned. Raising it back above the
#: faulthandler window recreates the silent 15-minute stalls.
#: No environment variable on purpose (one spelling per behaviour);
#: tests monkeypatch the constant.
_SQLITE_BUSY_TIMEOUT_S = 120.0


@dataclass
class _SaveTally:
    """What one `save_all` call wrote, for the summary log line."""
    patients: int = 0
    studies: int = 0
    series: int = 0
    instances: int = 0
    pixel_frames: int = 0
    pixel_bytes: int = 0


class _StoredFrame(NamedTuple):
    """Where an instance's pixels live in the sidecar, if anywhere.

    All four fields are None for an instance carrying no pixel data. The
    upsert COALESCEs them, so None means "leave whatever is stored alone"
    rather than "clear it".
    """
    offset: Optional[int]
    length: Optional[int]
    alg: Optional[str]
    hash: Optional[str]


def _as_stored_date(value) -> Optional[str]:
    """Renders a study date as text for SQLite.

    Python 3.12 deprecated the default date adapter, so dates are
    converted here rather than left for sqlite3 to guess at.
    """
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _as_loaded_date(value):
    """The inverse of `_as_stored_date`: text back into a `date`.

    Hydration had no inverse, so `Study.study_date` came back as the ISO
    string `_as_stored_date` wrote where `ingest` had produced a
    `datetime.date`. Nothing checks the type and both exporters read the
    field, so the same code emitted a legal (0008,0020) on a fresh
    session and `'2024-01-15'` -- not a DA value, which PS3.5 Table
    6.2-1 fixes at eight digits -- on a reloaded one, and WFDB's
    `_start_datetime` quietly fell through to the instance's own
    never-shifted date tags because its `strptime` raised inside a
    `try`. Restoring the type here rather than patching each exporter is
    what makes `study_date` one type everywhere; a normaliser at an
    export boundary leaves the next consumer meeting the same trap
    (#171).

    An unparseable value is returned as it was stored, not replaced:
    same rule as ingest's, where a date we cannot read is a date we do
    not have rather than one we invent (#60). NULL stays None for the
    same reason.

    `date.fromisoformat` also accepts the basic form `YYYYMMDD` on the
    3.12 floor, so a study whose date was set as a DICOM-spelled string
    rather than parsed at ingest normalizes to a `date` here too. That
    is deliberate and is the whole point -- one type everywhere. Do not
    "tighten" this to a strict `%Y-%m-%d` parse to keep such a value a
    string: that restores exactly the second type this exists to remove.

    The (0008,0020) *element* is unaffected by which spelling arrived,
    because either one loads as a `date` and both export paths render a
    `date` as `YYYYMMDD` -- `session.export()` by handing it to pydicom,
    `write_tree` via `format_study_date`. The exported *directory name*
    is not: `export_folder_names` builds it with
    `str(study.study_date or "NoDate")`, not `format_study_date`, so a
    hand-built graph carrying `"20240101"` files under `Study_20240101_`
    before a round trip and `Study_2024-01-01_` after one. Ingested
    studies never see this -- ingest already produces a `date`, so
    `str()` yields the ISO spelling on both sides -- and routing the
    folder through `format_study_date` would rename every existing
    export's directories, which is worse than the divergence it closes.
    Known and accepted, filed as #189; do not read the element's
    indifference as the folder's.
    """
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return value


def _split_core_and_private(attributes: Dict[str, Any]) -> Tuple[Dict[str, Any],
                                                                 Dict[Tuple[str, str], Any]]:
    """Separates private tags from the ones stored inline as JSON.

    Odd DICOM groups are private (PS3.5 §7.8) and go to the vertical
    `instance_attributes` table, where they can be queried per tag rather
    than by parsing every instance's JSON blob.

    Two things stay inline regardless: `__sequences__`, which is nested
    structure the vertical table has no shape for, and any `bytes` value,
    which that table's TEXT column cannot hold.

    Returns:
        Tuple of (core attributes keyed by "gggg,eeee", private attributes
        keyed by a ("gggg", "eeee") tuple).
    """
    core, private = {}, {}

    for key, value in attributes.items():
        if key == "__sequences__":
            core[key] = value
            continue

        try:
            group = int(key.split(',')[0], 16)
        except (ValueError, AttributeError):
            # Not a well-formed "gggg,eeee" pair; keep it as a standard
            # attribute rather than guessing at what it is.
            core[key] = value
            continue

        if group % 2 != 0 and not isinstance(value, bytes):
            private[tuple(key.split(','))] = value
        else:
            core[key] = value

    return core, private



def _report_abandoned_audit_rows(audit_queue):
    """Say that a collected store took undrained audit rows with it.

    This loss is **new** (#316): until the worker stopped pinning its
    store, a store with queued rows could not be collected at all. So it
    needs a channel, and the queue is held strongly by the worker
    precisely so the exit path can count what it is dropping rather than
    dropping it silently -- which is the one thing an audit log must
    never do.
    """
    pending = audit_queue.qsize()
    if pending:
        get_logger().warning(
            f"An SqliteStore was collected with {pending} audit row(s) "
            f"still queued; those rows are lost. Call stop() -- or close "
            f"the session -- to settle the audit log before dropping a "
            f"store (#316).")


def _audit_worker_loop(store_ref, stop_event, wakeup, audit_queue):
    """Background audit writer that does not keep its store alive.

    Module-level, taking a **weak** reference. `SqliteStore.__init__`
    used `target=self._audit_worker`, and a running `Thread` holds its
    target while a bound method holds `self` -- so every store ever
    constructed was immortal for as long as its worker ran, and the
    worker's only exit is `stop()`, which nothing calls on a store its
    owner simply dropped. Measured over one full suite run: 149 threads
    at interpreter exit, 147 of them audit writers, each holding a
    store, its sqlite handles and its sidecar descriptors. #250 fixed
    the same shape for `PersistenceManager`; this is the other half its
    argument named.

    **Why exiting on a dead weakref is safe, in one line.**
    `flush_audit_queue()` calls `_drain_and_write()` on the *caller's*
    thread, and `_drain_and_write` takes `_audit_write_lock` itself. The
    read barrier does not depend on this worker existing at all -- the
    worker only removes background latency. So a worker that exits
    cannot weaken the barrier, cannot invert `_audit_write_lock` ->
    `_memory_lock` (same locks, same order), and does not touch the
    untimed wait's semantics.

    Two things not to "simplify":

    - **`del store` before returning to the wait.** A strong reference
      held across the one-second wait on `wakeup` restores exactly the
      immortality this fixes -- for a second at a time, forever.
    - **The Events and the Queue are held strongly, and that is safe.**
      `threading.Event` references nothing, and audit rows are plain
      string tuples (see `log_audit`), so the queue holds no entity
      graph and no store.
    """
    while not stop_event.is_set():
        wakeup.wait(timeout=1.0)
        # Clear before draining, never after: a `put`+`set` landing
        # here has its `set` erased, but the row is in the queue
        # before the clear, so the drain below still takes it.
        wakeup.clear()
        store = store_ref()
        if store is None:
            _report_abandoned_audit_rows(audit_queue)
            return
        try:
            store._drain_and_write()
        except Exception as e:  # pylint: disable=broad-except
            # Don't crash thread
            store.logger.error(f"Audit Worker Error: {e}")
        finally:
            del store

    # Flush remaining
    store = store_ref()
    if store is not None:
        store._drain_and_write()


class SqliteStore:
    """
    Handles persistence of the Object Graph to a SQLite database.

    This class manages:
    - CRUD operations for the Patient->Study->Series->Instance hierarchy.
    - Sidecar retrieval and compaction logic.
    - An asynchronous Audit Log for tracking modifications and errors.
    """

    #: Rows `get_flattened_instances` fetches per page (#164).
    #:
    #: The knob trades resident memory against query count. A page is
    #: dominated by `attributes_json` -- every standard attribute of the
    #: instance, as text -- not by the sixteen scalar columns beside it,
    #: so the number that matters is roughly `page_size x blob size`.
    #: At 500 that is single-digit megabytes for ordinary CT metadata,
    #: which keeps the method's memory promise intact on the 100GB+
    #: datasets it exists for, while making the per-page cost (one rowid
    #: seek plus `page_size` primary-key joins) disappear into the noise.
    FLATTENED_PAGE_SIZE = 500

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS patients (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT NOT NULL,
        patient_name TEXT,
        phi_status TEXT,
        UNIQUE(patient_id)
    );

    CREATE TABLE IF NOT EXISTS studies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id_fk INTEGER,
        study_instance_uid TEXT NOT NULL,
        study_date TEXT,
        date_shifted INTEGER,
        phi_status TEXT,
        FOREIGN KEY(patient_id_fk) REFERENCES patients(id),
        UNIQUE(study_instance_uid)
    );

    CREATE TABLE IF NOT EXISTS series (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        study_id_fk INTEGER,
        series_instance_uid TEXT NOT NULL,
        modality TEXT,
        series_number INTEGER,
        manufacturer TEXT,
        model_name TEXT,
        device_serial_number TEXT,
        FOREIGN KEY(study_id_fk) REFERENCES studies(id),
        UNIQUE(series_instance_uid)
    );

    CREATE TABLE IF NOT EXISTS instances (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        series_id_fk INTEGER,
        sop_instance_uid TEXT NOT NULL,
        sop_class_uid TEXT,
        instance_number INTEGER,
        file_path TEXT,
        source_path TEXT,
        pixel_file_id INTEGER DEFAULT 0,
        pixel_offset INTEGER,
        pixel_length INTEGER,
        pixel_hash TEXT,
        compress_alg TEXT,
        attributes_json TEXT, -- Core attributes (Horizontal)
        phi_status TEXT,      -- What the last scan concluded, if still valid
        FOREIGN KEY(series_id_fk) REFERENCES series(id),
        UNIQUE(sop_instance_uid)
    );

    CREATE TABLE IF NOT EXISTS instance_attributes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        instance_uid TEXT NOT NULL,
        group_id TEXT NOT NULL,
        element_id TEXT NOT NULL,
        atom_index INTEGER DEFAULT 0,
        value_rep TEXT,
        value_text TEXT,
        FOREIGN KEY(instance_uid) REFERENCES instances(sop_instance_uid) ON DELETE CASCADE,
        UNIQUE(instance_uid, group_id, element_id, atom_index)
    );

    CREATE TABLE IF NOT EXISTS instance_blobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        instance_uid TEXT NOT NULL,
        kind TEXT NOT NULL,
        file_id INTEGER DEFAULT 0,
        offset INTEGER,
        length INTEGER,
        hash TEXT,
        compress_alg TEXT,
        UNIQUE(instance_uid, kind)
    );

    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT,
        action_type TEXT,
        entity_uid TEXT,
        details TEXT,
        -- Set only on DATA_LOSS rows, by the emitter, and read by
        -- `generate_report` to grade the run (#146). NULL everywhere
        -- else, and on DATA_LOSS rows written before this column
        -- existed -- those cannot be graded and are not guessed at.
        loss_scope TEXT,
        -- Set only on SCAN_GAP rows, by the emitter: the `gggg,eeee`
        -- of the element the parse gate refused. `generate_report`
        -- resolves it against the object graph to say whether that
        -- element is still held for export, which is what the row's
        -- section header claims and what the grade turns on (#167).
        -- The tag is stored rather than read back out of `details`
        -- for the reason `loss_scope` is: only the emitter still holds
        -- it, and re-deriving it from prose is a second answer to
        -- "which element is this".
        element_tag TEXT
    );
    CREATE TABLE IF NOT EXISTS phi_findings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT,
        entity_uid TEXT,
        entity_type TEXT,
        field_name TEXT,
        value TEXT,
        reason TEXT,
        patient_id TEXT,
        remediation_action TEXT,
        remediation_value TEXT,
        details_json TEXT
    );

    -- Indexing for Performance
    CREATE INDEX IF NOT EXISTS idx_studies_patient_fk ON studies(patient_id_fk);
    CREATE INDEX IF NOT EXISTS idx_series_study_fk ON series(study_id_fk);
    CREATE INDEX IF NOT EXISTS idx_instances_series_fk ON instances(series_id_fk);
    CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity_uid);
    CREATE INDEX IF NOT EXISTS idx_findings_entity ON phi_findings(entity_uid);
    CREATE INDEX IF NOT EXISTS idx_inst_attr_uid ON instance_attributes(instance_uid);
    CREATE INDEX IF NOT EXISTS idx_blobs_uid_kind ON instance_blobs(instance_uid, kind);
    """

    def __init__(self, db_path: str):
        """
        Initialize the SQLite store.

        Args:
            db_path (str): Path to the SQLite DB file. Use ":memory:" for transient storage.
        """
        self.db_path = db_path
        self.logger = get_logger()
        if db_path == ":memory:":
            # Use a temporary file for sidecar if DB is in-memory
            # SidecarManager currently requires a file path (append-only logic)
            # Create a temp file that persists until process exit (or manual cleanup)
            # We use NamedTemporaryFile but close it so SidecarManager can open/lock it.
            tf = tempfile.NamedTemporaryFile(suffix="_pixels.bin", delete=False)
            self.sidecar_path = tf.name
            tf.close()
            # Shared memory connection for :memory: database to persist across transactions
            self._memory_conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._memory_conn.row_factory = sqlite3.Row
            self._memory_lock = threading.Lock()
        else:
            self.sidecar_path = os.path.splitext(db_path)[0] + "_pixels.bin"
            self._memory_conn = None
            self._memory_lock = None

        self.sidecar = SidecarManager(self.sidecar_path)
        self._init_db()

        # Async Audit Queue
        self.audit_queue = queue.Queue()
        self._stop_event = threading.Event()
        # Both must exist before the worker starts -- it touches them on
        # its first tick (#218).
        self._audit_write_lock = threading.Lock()
        self._audit_wakeup = threading.Event()
        # Rows a failed `log_audit_batch` dropped. Its own lock, not
        # `_audit_write_lock`: the increment happens inside
        # `log_audit_batch`, which the worker calls while already
        # holding the write lock (non-reentrant), and which
        # `remediation.py` calls holding nothing.
        self._audit_drop_lock = threading.Lock()
        self._audit_rows_dropped = 0
        # At most one writer may traverse "read this instance's pixel
        # bytes -> publish a loader for them" at a time. Two writers
        # exist -- the background save (`_persist_pixels`) and the
        # redaction swap (`persist_pixel_data`) -- and without mutual
        # exclusion the save can capture pre-redaction bytes and rebind
        # `_pixel_loader`/`_pixel_hash` *after* the redacted frame was
        # bound: the instance then reads back unredacted pixels under a
        # full redaction attestation, self-consistently, because the
        # stale hash matches the stale frame (#274). Lock order:
        # `_pixel_swap_lock` before `sidecar._lock`, never reversed; and
        # never held across a sqlite write, whose busy timeout can be
        # waited out while holding it.
        self._pixel_swap_lock = threading.Lock()
        self._audit_thread = threading.Thread(
            target=_audit_worker_loop,
            args=(weakref.ref(self), self._stop_event, self._audit_wakeup,
                  self.audit_queue),
            daemon=True, name="AuditWorker")
        self._audit_thread.start()

    def __getstate__(self):
        """Exclude threading primitives from pickling."""
        state = self.__dict__.copy()
        keys_to_remove = [
            '_memory_lock',
            '_memory_conn',
            'audit_queue',
            '_stop_event',
            # A lock and an Event both raise `TypeError: cannot pickle
            # '_thread.lock' object`. Adding an audit primitive without
            # adding it here breaks *every* pickle of a store (#218).
            '_audit_write_lock',
            '_audit_wakeup',
            '_audit_drop_lock',
            '_pixel_swap_lock',
            '_audit_thread']
        for k in keys_to_remove:
            state.pop(k, None)
        return state

    def __setstate__(self, state):
        """Recreate threading primitives on unpickling."""
        self.__dict__.update(state)

        # Restore non-pickleable attributes
        if self.db_path == ":memory:":
            self._memory_lock = threading.Lock()
            self._memory_conn = None  # Connection lost on pickle transfer
        else:
            self._memory_lock = None
            self._memory_conn = None

        self.audit_queue = queue.Queue()
        self._stop_event = threading.Event()
        # See `__init__`: before the worker starts, not after (#218).
        self._audit_write_lock = threading.Lock()
        self._audit_wakeup = threading.Event()
        self._audit_drop_lock = threading.Lock()
        self._pixel_swap_lock = threading.Lock()
        self._audit_thread = threading.Thread(
            target=_audit_worker_loop,
            args=(weakref.ref(self), self._stop_event, self._audit_wakeup,
                  self.audit_queue),
            daemon=True, name="AuditWorker")
        self._audit_thread.start()

    @contextlib.contextmanager
    def _get_connection(self):
        """
        Context manager for database connections.
        Handles persistent connection for :memory: databases.
        """
        if self._memory_conn:
            # For in-memory DB, reuse the single connection.
            # We must serialize access because sqlite3 connections are not thread-safe
            # for concurrent writes even with check_same_thread=False.
            with self._memory_lock:
                try:
                    # print(f"DEBUG: Acquired lock. Yielding conn {id(self._memory_conn)}") #
                    # Reduced spam
                    yield self._memory_conn
                    self._memory_conn.commit()
                    # print("DEBUG: Commit successful")
                except Exception as e:
                    # print(f"DEBUG: Rollback due to {e}")
                    self._memory_conn.rollback()
                    raise e
        else:
            # File-based DB: create fresh connection per transaction
            conn = sqlite3.connect(self.db_path, timeout=_SQLITE_BUSY_TIMEOUT_S)
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.commit()
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _init_db(self):
        with self._get_connection() as conn:
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA auto_vacuum = FULL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.executescript(self.SCHEMA)
            self._add_missing_columns(conn)
            self._backfill_legacy_blobs(conn)

    @staticmethod
    def _add_missing_columns(conn):
        """Adds columns introduced after a database was first created.

        `CREATE TABLE IF NOT EXISTS` leaves an existing table exactly as it
        was, so a column added to the schema never appears in a store an
        earlier version created. Each ALTER is guarded by the table's own
        column list rather than a version number, which keeps this
        idempotent and independent of how the database got here.

        Rows predating the column read as NULL, which `_phi_status_from_stored`
        maps to UNSCANNED -- correct, since they were never scanned under
        this scheme.
        """
        for table in ("patients", "studies", "instances"):
            columns = {row[1] for row in conn.execute(
                f"PRAGMA table_info({table})").fetchall()}
            if "phi_status" not in columns:
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN phi_status TEXT")

        # `loss_scope` on audit_log (#146). A DATA_LOSS row written
        # before this column existed reads NULL, and NULL is ungraded:
        # the scope says what kind of element was dropped, and the only
        # place that ever knew is the emitter that has long since run.
        # Back-filling it by parsing `details` is exactly the coupling
        # the column exists to avoid.
        audit_columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(audit_log)").fetchall()}
        if "loss_scope" not in audit_columns:
            conn.execute("ALTER TABLE audit_log ADD COLUMN loss_scope TEXT")

        # `element_tag` on audit_log (#167), by the same argument. A
        # SCAN_GAP row written before this column reads NULL, and NULL
        # is unresolved: nothing here can know which element it named,
        # so the report says so and the run keeps its REVIEW_REQUIRED
        # rather than being graded on a guess.
        if "element_tag" not in audit_columns:
            conn.execute("ALTER TABLE audit_log ADD COLUMN element_tag TEXT")

        # `source_path` on instances (#238). Rows predating the column
        # read NULL; for an un-redacted instance `Instance.__post_init__`
        # re-derives it from `file_path` on load, so only instances
        # already redacted in an older release stay without provenance
        # -- their `file_path` was cleared before anything recorded it,
        # and nothing here can recover it.
        instance_columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(instances)").fetchall()}
        if "source_path" not in instance_columns:
            conn.execute("ALTER TABLE instances ADD COLUMN source_path TEXT")

        # `date_shifted` on studies (#182). SHIFT_DATE sets the flag and
        # the WFDB exporter reads it to decide whether the header's date
        # comment may say "de-identified"; without a column every save
        # dropped it and a reloaded export declined to claim a genuine
        # de-identification. Rows predating the column read NULL, which
        # hydrates as False -- correct, not a loss: the old schema never
        # recorded whether a date was shifted, and a provenance claim the
        # store cannot back must not be fabricated (the same direction
        # the exporter's own comment enforces).
        study_columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(studies)").fetchall()}
        if "date_shifted" not in study_columns:
            conn.execute("ALTER TABLE studies ADD COLUMN date_shifted INTEGER")

    def _backfill_legacy_blobs(self, conn):
        """Migrate 0.6.x pixel_* columns into instance_blobs.

        Idempotent: INSERT OR IGNORE means rows already migrated, or written
        by the current code path, are left untouched. The legacy columns are
        deliberately left in place so a downgrade still reads correctly.

        Args:
            conn (sqlite3.Connection): An already-open connection. Passed in
                rather than acquired here because callers run inside their own
                transaction (and the :memory: connection lock is not
                re-entrant).
        """
        conn.execute("""
            INSERT OR IGNORE INTO instance_blobs
                (instance_uid, kind, file_id, offset, length, hash, compress_alg)
            SELECT sop_instance_uid, 'pixels', COALESCE(pixel_file_id, 0),
                   pixel_offset, pixel_length, pixel_hash, compress_alg
            FROM instances
            WHERE pixel_offset IS NOT NULL AND pixel_length IS NOT NULL
        """)

    def _create_pixel_loader(self, offset, length, alg, instance, pixel_hash=None):
        """Helper to create a lazy pixel loader for the sidecar."""
        # Use instance to populate primitives
        return SidecarPixelLoader(self.sidecar_path, offset, length, alg, instance=instance, pixel_hash=pixel_hash)

    def _wire_waveform_loader(self, instance, wref):
        """Attach a lazy waveform loader to a freshly hydrated Instance.

        Shared by `load_all` and `load_patient`, which hydrate the same rows
        through two separate loops.

        Also heals a legacy multiplex shape on the way past -- see
        `_prune_hollow_multiplex_items`.

        Args:
            instance (Instance): The hydrated instance; its attributes and
                sequences must already be restored, because the loader reads
                its geometry out of the Waveform Sequence.
            wref: A row from `instance_blobs` (kind 'waveform'), or None.
        """
        # Before the `wref is None` return: the damaged shape can exist
        # with no waveform blob at all (a source whose group 0 carried
        # no samples), and its export is exactly as hollow.
        self._prune_hollow_multiplex_items(instance)

        if wref is None:
            return

        from .io_handlers import SidecarWaveformLoader

        instance._waveform_hash = wref['hash']
        try:
            instance._waveform_loader = SidecarWaveformLoader(
                self.sidecar_path, wref['offset'], wref['length'],
                wref['compress_alg'], instance=instance,
                waveform_hash=wref['hash'])
        except ValueError:
            # Geometry lives in the Waveform Sequence, which is restored
            # from attributes_json above. A missing sequence means a
            # corrupt row, not a fatal error.
            self.logger.warning(
                f"Waveform blob for {instance.sop_instance_uid} has no "
                "Waveform Sequence; skipping loader.")

    def _prune_hollow_multiplex_items(self, instance):
        """Heal a pre-#160 store: drop multiplex items that have no samples.

        A store indexed before the #160 fix holds one Waveform Sequence
        (5400,0100) item per multiplex group while the sidecar holds
        group 0's samples alone -- ingest discarded groups 1..n (#36)
        and `populate_attrs` kept their metadata anyway. `ingest_worker`
        never runs again on an existing index, so without this the
        export writes every item back and declares a multiplex group
        with no Waveform Data (5400,1010), a Type 1 element (#168).

        Pruned at hydration rather than at export, for #160's own
        reason: the graph is what every consumer reads -- the DICOM
        writer, the WFDB record, the annotation bridge, the PHI scan --
        and a writer that quietly drops items is a second answer to
        "which multiplex groups does this record have". This edits a
        graph the user did not ask to have edited, so it is a logged
        warning naming the instance and the remedy; it is NOT an audit
        row, because the loss it describes was already audited by the
        session that ingested (0.8.2 onward writes the DATA_LOSS entry
        into this same store's audit_log), and this heals the graph to
        agree with what that log already says.

        The annotations referencing the pruned items go with them,
        through the same filter ingest uses (#177) -- pruning the item
        alone would unmask exactly the dangling ordinal that filter
        exists to prevent. `DicomExporter.write_tree()` on a hand-built
        graph is deliberately NOT covered: the serializer applies no
        gates by design, and there is no store -- and no earlier
        session's audit trail -- anywhere in that picture.

        The prune must not look like an edit: items and references are
        removed with direct container mutation, never `set_attr`, so
        `_revision` stays put, the stored `phi_status` survives, and the
        graph still reads as clean (`_apply_vertical_attributes`
        documents the same invariant). The store itself is untouched
        until the user saves, so the warning repeats on every open of an
        unhealed store -- which is the correct amount of loud for a
        graph being changed under its owner.
        """
        seq = instance.sequences.get("5400,0100")
        if seq is None or len(seq.items) <= 1:
            return

        pruned = len(seq.items) - 1
        del seq.items[1:]

        from .waveform import filter_dangling_annotation_refs
        ann_dropped, ann_rewritten, _groups = filter_dangling_annotation_refs(
            instance, kept_items=len(seq.items))

        ann_note = ""
        if ann_dropped or ann_rewritten:
            ann_note = (
                f" {ann_dropped} waveform annotation(s) referencing the "
                f"pruned groups were dropped and {ann_rewritten} trimmed "
                f"to their surviving references (#177).")
        self.logger.warning(
            f"{instance.sop_instance_uid}: Waveform Sequence held "
            f"{pruned + 1} multiplex groups but this store carries "
            f"samples for group 0 only -- it was indexed before the "
            f"#160 fix, which discarded the samples and kept the "
            f"metadata. Pruned {pruned} sample-less item(s) so the "
            f"export does not declare Waveform Data it cannot carry "
            f"(Type 1, PS3.3 C.10.9).{ann_note} The discarded samples "
            f"are not recoverable from this store; to get them back, "
            f"re-ingest the original files into a fresh index.")

    def _drain_and_write(self):
        """Move every currently queued audit row into the database.

        The only place rows leave `audit_queue`, and the lock is what
        makes `flush_audit_queue` a barrier rather than a hopeful drain
        (#218). Rows leave the queue only under `_audit_write_lock` and
        are in the database before it is released, so a row is never
        owned by a local variable a reader cannot see. The worker used
        to `get()` rows into its own local `batch` and write them later;
        between those two points a row was in neither the queue nor the
        table, and a reader that "flushed" found nothing to do and
        selected without it.

        `log_audit_batch` must never acquire `_audit_write_lock`: it is
        called here *while holding it*, and `threading.Lock` is not
        reentrant, so a defensive acquire would self-deadlock on the
        first row.
        """
        while True:
            with self._audit_write_lock:
                batch = []
                while len(batch) < 100:
                    try:
                        batch.append(self.audit_queue.get_nowait())
                    except queue.Empty:
                        break
                if batch:
                    self.log_audit_batch(batch)
            # A full batch means there may be more behind it; anything
            # short means the queue went empty under the lock, which is
            # the barrier's guarantee and the loop's exit.
            if len(batch) < 100:
                return

    def stop(self):
        """Stops the audit worker and flushes queue."""
        self._stop_event.set()
        # Wake the worker now instead of letting it wait out its 1.0 s
        # tick, so the join below rarely has to fire at all.
        self._audit_wakeup.set()
        if self._audit_thread.is_alive():
            self._audit_thread.join(timeout=2.0)
        # A timed-out join no longer loses rows: this waits out any
        # in-flight write on the lock and drains the rest itself.
        self.flush_audit_queue()

    def flush_audit_queue(self):
        """Settle the audit log.

        Returns only when every row enqueued before this call is
        readable from `audit_log`. This is a barrier, not a poll.

        Before #218 it drained the queue and returned, which said
        nothing about rows the worker had already taken out of the
        queue and not yet written. A compliance report reading through
        it graded `PASS` a run that dropped a private tag, because the
        `DATA_LOSS`/`PRIVATE` row was in the worker's local batch when
        `get_audit_losses()` looked.

        There is deliberately no timeout. A bounded barrier would
        reintroduce `stop()`'s failure mode -- a compliance read that
        quietly gives up cannot be told apart from one that found
        nothing. The worst case is one `log_audit_batch`; producers
        never hold the lock, so no volume of logging can extend a
        single acquisition.
        """
        self._drain_and_write()

    def log_audit(self, action_type: str, entity_uid: str, details: str,
                  loss_scope: Optional[str] = None,
                  element_tag: Optional[str] = None):
        """Records an action in the audit log (Async).

        Args:
            action_type (str): e.g. 'EXPORT', 'ERROR', 'DATA_LOSS'.
            entity_uid (str): The instance (or path) the action concerns.
            details (str): Prose for the human reading the report.
            loss_scope (str, optional): For `DATA_LOSS` only:
                `io_handlers.LOSS_SCOPE_PRIVATE`, `LOSS_SCOPE_STANDARD`
                or `LOSS_SCOPE_SIGNAL`. This is what `generate_report`
                grades on, and it is passed in rather than derived from
                `details` because only the caller still holds the tag
                (#146).
            element_tag (str, optional): For `SCAN_GAP` only: the
                `gggg,eeee` the parse gate refused. `generate_report`
                resolves it against the object graph to say whether the
                element is still held for export (#167). Passed in for
                the same reason `loss_scope` is.
        """
        # Push to queue instead of writing directly. Producers take
        # neither lock and are never blocked by a database write.
        self.audit_queue.put(
            (action_type, entity_uid, details, loss_scope, element_tag))
        self._audit_wakeup.set()

    def get_audit_summary(self) -> Dict[str, int]:
        """
        Returns an aggregated summary of actions from the audit log.

        It used to `stop()` the worker and restart it in a `finally`,
        which was not a barrier but a race with a two-second head start
        (#218). When the join timed out the caller got `{}` for a store
        with rows recorded, silently, and the restart started a second
        worker while the first was still alive -- one leaked thread per
        timed-out read. Both are gone: this reads through the barrier
        and starts nothing.

        Returns:
            Dict[str, int]: e.g., {'ANONYMIZE': 500, 'EXPORT': 500}
        """
        # Above the connection, never inside it: the lock order is
        # `_audit_write_lock` -> `_memory_lock`, and flushing from
        # within `_get_connection` would invert it on a `:memory:`
        # store and deadlock.
        self.flush_audit_queue()

        with self._get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "SELECT action_type, COUNT(*) FROM audit_log GROUP BY action_type")
                rows = cursor.fetchall()
                return {row[0]: row[1] for row in rows}
            except sqlite3.OperationalError:
                return {}

    def get_audit_errors(self) -> List[tuple]:
        """
        Retrieves all audit logs with type ERROR or WARNING.
        Returns:
            List[tuple]: (timestamp, action_type, details)
        """
        self.flush_audit_queue()
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT timestamp, action_type, details
                    FROM audit_log
                    WHERE action_type IN ('ERROR', 'WARNING')
                    ORDER BY timestamp ASC
                """)
                return cursor.fetchall()
        except sqlite3.OperationalError:
            return []

    def get_audit_losses(self) -> List[tuple]:
        """
        Retrieves every `DATA_LOSS` entry, with the scope it was
        recorded under.

        Still separate from `get_audit_errors`, and the reason is no
        longer that the grade is untouched -- it is not. A loss scoped
        `PRIVATE` or `SIGNAL` takes `validation_status` to
        `REVIEW_REQUIRED`; one scoped `STANDARD` leaves it at `PASS`
        (CHANGELOG.md, #146 and #150).
        Folding these rows into `get_audit_errors` would grade all of
        them alike *and* file a routine drop under "Exceptions &
        Errors", where nothing failed.

        A row whose `loss_scope` is NULL predates the column and cannot
        be graded; it is reported and left at `PASS`.

        Returns:
            List[tuple]: (timestamp, entity_uid, details, loss_scope)
        """
        self.flush_audit_queue()
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT timestamp, entity_uid, details, loss_scope
                    FROM audit_log
                    WHERE action_type = 'DATA_LOSS'
                    ORDER BY timestamp ASC
                """)
                return cursor.fetchall()
        except sqlite3.OperationalError:
            return []

    def get_audit_scan_gaps(self) -> List[tuple]:
        """Every `SCAN_GAP` entry: an element the PHI scan could not open.

        Separate from `get_audit_losses` because it is a different
        claim. A loss says an element was dropped at ingest and cannot
        reach the output; this says an element was kept whole and the
        scan could not read what is inside it (#167). Folding them
        together would file one under a section header that denies it.

        The row states ingest-time knowledge only. Whether the element
        reaches the exported file is decided later, by
        `remove_private_tags`, and `generate_report` resolves that
        against the object graph -- the row itself must not claim it
        (#167).

        No `loss_scope` column: these are private by construction --
        only an odd-group tag reaches the parse gate -- so the column
        would hold one value and grade nothing. `element_tag` is
        selected instead, and is NULL for a row written before that
        column existed.

        Returns:
            List[tuple]: (timestamp, entity_uid, details, element_tag)
        """
        self.flush_audit_queue()
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT timestamp, entity_uid, details, element_tag
                    FROM audit_log
                    WHERE action_type = 'SCAN_GAP'
                    ORDER BY timestamp ASC
                """)
                return cursor.fetchall()
        except sqlite3.OperationalError:
            return []

    def get_audit_drops(self) -> int:
        """How many audit rows were dropped by a failed batch write.

        The rows themselves are unrecoverable -- see `log_audit_batch`
        for why they are counted rather than retried (#219). A non-zero
        count means the audit table under-states what happened, which
        is why `generate_report` grades it like an exception rather
        than mentioning it: an audit trail with holes cannot support a
        PASS.

        Flushes first, like every other audit reader: a row still in
        the queue has not met the failing write yet, so counting before
        the barrier would miss it.
        """
        self.flush_audit_queue()
        with self._audit_drop_lock:
            return self._audit_rows_dropped

    def check_unsafe_attributes(self) -> List[tuple]:
        """
        Scans for instances with potentially unsafe attributes (e.g., BurnedInAnnotation="YES").
        Returns:
            List[tuple]: (sop_instance_uid, file_path, details)
        """
        unsafe = []
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                # Naive text search in JSON.
                # matches "0028,0301": "YES"
                # We need to be careful about spacing in JSON serialization, but standard json.dumps usually does ": "
                # A safer broad check is %0028,0301%YES%
                cursor.execute("""
                    SELECT sop_instance_uid, file_path
                    FROM instances
                    WHERE attributes_json LIKE '%"0028,0301": "YES"%'
                """)
                rows = cursor.fetchall()
                for r in rows:
                    unsafe.append((r[0], r[1], "BurnedInAnnotation FLAGGED as YES"))
        except sqlite3.OperationalError:
            pass
        return unsafe

    def check_pixel_geometry(self) -> List[tuple]:
        """Instances whose stored descriptors cannot describe their frame.

        The detector for stores #186 already damaged before its fix
        landed: the defect persisted a guessed geometry (RGB, 3 samples,
        swapped axes) for multi-frame grayscale instances, and a store
        carrying it exports garbage while grading PASS -- every step
        downstream behaves correctly on descriptors that are already
        wrong (#214). Repair is deliberately not attempted: the
        sidecar's bytes are shape-free, so a migration would be
        best-effort, and a best-effort repair that silently half-works
        is worse than a detector. The remedy is the caller's -- re-ingest
        from source, or `export(verify_readback=True)` (#209) -- and
        rides the warning `DicomSession.__init__` logs from this result.

        The check is arithmetic and exact: Rows x Columns x
        SamplesPerPixel x NumberOfFrames x bytes-per-sample must equal
        the stored frame length. Bytes-per-sample mirrors
        `SidecarPixelLoader`'s dtype bucketing (`uint16 if bits > 8 else
        uint8`) rather than BitsAllocated/8, because the sidecar holds
        `pixel_array.tobytes()` -- a 1-bit Segmentation is stored
        expanded to uint8, and dividing its declared width by 8 would
        flag every healthy one.

        **Scope: frames stored uncompressed only.** A zlib frame's
        stored length is post-compression, so the equality holds for no
        store, damaged or healthy, and deciding it by decompressing
        every frame would read the whole sidecar on every open -- the
        memory-scaling promise says no. A frame whose `compress_alg` is
        NULL is skipped too: its encoding is unrecorded and nothing here
        guesses. Damage hiding behind a compressed frame is caught where
        the bytes are actually decoded, by `verify_readback` at export.

        Returns:
            List[tuple]: (sop_instance_uid, file_path, details), the
            same shape as `check_unsafe_attributes` so `generate_report`
            files both through one channel.
        """
        flagged = []
        try:
            with self._get_connection() as conn:
                rows = conn.execute("""
                    SELECT sop_instance_uid, file_path, pixel_length,
                           attributes_json
                    FROM instances
                    WHERE pixel_offset IS NOT NULL
                      AND pixel_length IS NOT NULL
                      AND compress_alg = 'raw'
                """).fetchall()
        except sqlite3.OperationalError:
            return []

        for r in rows:
            try:
                attrs = json.loads(r['attributes_json'] or "{}")
            except (json.JSONDecodeError, TypeError):
                continue

            def _as_dim(tag, default=None, attrs=attrs):
                value = attrs.get(tag, default)
                if isinstance(value, list):
                    value = value[0] if value else default
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return None

            pixel_rows = _as_dim("0028,0010")
            pixel_cols = _as_dim("0028,0011")
            samples = _as_dim("0028,0002", 1)
            frames = _as_dim("0028,0008", 1)
            bits = _as_dim("0028,0100", 8)
            if not pixel_rows or not pixel_cols or not samples or not bits:
                # Descriptors this incomplete cannot be graded either
                # way; the loader will fail loudly on its own terms.
                continue
            frames = max(frames or 1, 1)

            bytes_per_sample = 2 if bits > 8 else 1
            expected = (pixel_rows * pixel_cols * samples * frames
                        * bytes_per_sample)
            if expected != r['pixel_length']:
                flagged.append((
                    r['sop_instance_uid'], r['file_path'],
                    f"Stored pixel geometry cannot describe the stored "
                    f"frame: Rows={pixel_rows} Columns={pixel_cols} "
                    f"SamplesPerPixel={samples} NumberOfFrames={frames} "
                    f"BitsAllocated={bits} implies {expected} bytes, "
                    f"but the sidecar frame holds {r['pixel_length']}. "
                    f"The descriptors were likely rewritten by a "
                    f"pre-fix release (#186); an export of this "
                    f"instance is not trustworthy. Re-ingest the "
                    f"source file, or run export(verify_readback=True) "
                    f"to fail it at delivery (#209)."))
        return flagged

    def log_audit_batch(self, entries: List[tuple]):
        """
        Batch inserts audit logs.

        entries: List of
        (action_type, entity_uid, details, loss_scope, element_tag).
        `loss_scope` is None for everything that is not a `DATA_LOSS`
        row and `element_tag` for everything that is not a `SCAN_GAP`
        one; a caller with neither to describe still writes both slots,
        because one record with several accepted shapes is a fork the
        reader has to hold in their head.

        A batch that fails to insert -- for any reason, sqlite or not --
        is *dropped and counted*, never retried and never raised (#219).
        Retrying would mean holding the rows somewhere: a local survives
        no reader's barrier (that was #218's defect), and re-enqueueing
        under the lock loops forever on a permanently failing write and
        reorders the log besides. Raising is no better -- this used to
        swallow `sqlite3.Error` into a log line while the worker's
        `except` swallowed the rest, and both were the same silent
        under-report. The count is the one trace that reaches a reader:
        `generate_report` files a non-zero `get_audit_drops()` as an
        exception, which costs the run its PASS.
        """
        if not entries:
            return

        try:
            timestamp = datetime.now().isoformat()
            # (timestamp, action, uid, details, loss_scope, element_tag)
            data = [(timestamp, e[0], e[1], e[2], e[3], e[4]) for e in entries]

            with self._get_connection() as conn:
                conn.executemany(
                    "INSERT INTO audit_log (timestamp, action_type, entity_uid, "
                    "details, loss_scope, element_tag) "
                    "VALUES (?, ?, ?, ?, ?, ?)", data)
                conn.commit()
        except Exception as e:  # pylint: disable=broad-except
            # `_audit_drop_lock`, never `_audit_write_lock`: the worker
            # calls this while holding the write lock, which is not
            # reentrant.
            with self._audit_drop_lock:
                self._audit_rows_dropped += len(entries)
            self.logger.error(
                f"Failed to batch log audit; {len(entries)} row(s) "
                f"dropped: {e}")

    def load_all(self) -> List[Patient]:
        """
        Reconstructs the entire object graph from the database.

        Fetches all patients, studies, series, and instances, and reassembles them
        into the proper object hierarchy.

        Returns:
            List[Patient]: A list of all root Patient objects.
        """
        patients = []
        if self.db_path != ":memory:" and not os.path.exists(self.db_path):
            return patients

        try:
            with self._get_connection() as conn:
                # conn.row_factory = sqlite3.Row  <-- Handled by _get_connection
                cur = conn.cursor()

                # Optimized: We could do joins, but for clarity/mapping let's do hierarchical fetch.
                # Or fetch all and Stitch. Stitching in memory is faster for SQLite than
                # N+1 queries.

                # 1. Fetch AlL
                p_rows = cur.execute("SELECT * FROM patients").fetchall()
                st_rows = cur.execute("SELECT * FROM studies").fetchall()
                se_rows = cur.execute("SELECT * FROM series").fetchall()
                i_rows = cur.execute("SELECT * FROM instances").fetchall()

                # 2. Build Maps
                # Statuses are applied after hydration, not during it:
                # setting attributes advances an entity's revision, and a
                # status stamped at an earlier one would read as stale the
                # moment anyone asked.
                stored_statuses = []

                p_map = {}
                for r in p_rows:
                    p = Patient(r['patient_id'], r['patient_name'])
                    p_map[r['id']] = p
                    patients.append(p)
                    stored_statuses.append((p, r['phi_status']))

                st_map = {}
                for r in st_rows:
                    st = Study(r['study_instance_uid'], _as_loaded_date(r['study_date']))
                    # NULL (a row from before the column, #182) and 0 both
                    # read False: only a store that recorded the shift may
                    # claim one.
                    st.date_shifted = bool(r['date_shifted'])
                    st_map[r['id']] = st
                    stored_statuses.append((st, r['phi_status']))
                    if r['patient_id_fk'] in p_map:
                        p_map[r['patient_id_fk']].studies.append(st)

                # Waveforms have no columns on `instances`, so their only
                # record is the blob table. Fetched once here rather than
                # per instance to keep hydration a fixed number of queries.
                wave_refs = {
                    row['instance_uid']: row
                    for row in cur.execute(
                        "SELECT instance_uid, offset, length, compress_alg, hash"
                        " FROM instance_blobs WHERE kind = 'waveform'").fetchall()
                }

                # The private tier, in one query for the whole store. Its
                # rows are the odd-group tags `_split_core_and_private`
                # kept out of `attributes_json`; nothing read them back
                # until #158, so `remove_private_tags=False` was honoured
                # only until the session was closed. Pre-fetched here for
                # the same reason as `wave_refs` above: per-instance would
                # be one query per instance on every session open.
                vertical = self.load_vertical_attributes_bulk(conn=conn)

                se_map = {}
                for r in se_rows:
                    se = Series(r['series_instance_uid'], r['modality'], r['series_number'])
                    if r['manufacturer'] or r['model_name']:
                        se.equipment = Equipment(
                            r['manufacturer'], r['model_name'], r['device_serial_number'])
                    se_map[r['id']] = se
                    if r['study_id_fk'] in st_map:
                        st_map[r['study_id_fk']].series.append(se)

                for r in i_rows:
                    inst = Instance(
                        r['sop_instance_uid'],
                        r['sop_class_uid'],
                        r['instance_number'],
                        file_path=r['file_path']
                    )
                    # After construction, so a stored value wins over the
                    # `file_path` derivation in `__post_init__`. For a
                    # redacted instance `file_path` is NULL and this is
                    # the only thing that brings its origin back; without
                    # it the field is memory-only, ingest de-duplication
                    # is correct in the session that redacted and wrong
                    # in every session that reopens the store (#238).
                    if r['source_path']:
                        inst.source_path = r['source_path']

                    # Restore extra attributes
                    if r['attributes_json']:
                        try:
                            attrs = json.loads(
                                r['attributes_json'], object_hook=isocenter_json_object_hook)
                            self._deserialize_into(inst, attrs)
                        except (json.JSONDecodeError, TypeError) as exc:
                            self.logger.error(
                                "Could not decode stored attributes for "
                                "instance %s: %s", r['sop_instance_uid'], exc)

                    self._apply_vertical_attributes(
                        inst, vertical.get(r['sop_instance_uid'], {}))

                    # Wire up Sidecar Loader if present
                    if r['pixel_offset'] is not None and r['pixel_length'] is not None:

                        # We need to reshape after loading. The dimensions are in attributes.
                        # We can do this inside the lambda wrapper or a helper method.
                        # But Instance.attributes aren't populated yet!
                        # Wait, we populate attributes right after this.
                        # So the lambda calls self.instance methods? No, lambda binds early.

                        inst._pixel_loader = self._create_pixel_loader(
                            r['pixel_offset'], r['pixel_length'], r['compress_alg'], inst)

                    self._wire_waveform_loader(inst, wave_refs.get(r['sop_instance_uid']))

                    if r['series_id_fk'] in se_map:
                        se_map[r['series_id_fk']].instances.append(inst)

                    stored_statuses.append((inst, r['phi_status']))

            self.logger.info(f"Loaded {len(patients)} patients from {self.db_path}")

            # The row that was loaded is the row the status was written for,
            # so the stored conclusion applies to this revision. Recorded
            # before marking persisted, because recording advances the
            # revision.
            for entity, stored in stored_statuses:
                entity.record_phi_status(_phi_status_from_stored(stored))

            # Mark all loaded data as clean so we don't save it back immediately
            for p in patients:
                p.mark_subtree_persisted()
            return patients

        except sqlite3.Error as e:
            # print(f"DEBUG: Failed to load from DB: {e}")
            self.logger.error(f"Failed to load PDF from DB: {e}")
            traceback.print_exc()
            return []

    def load_patient(self, patient_uid: str) -> Optional[Patient]:
        """
        Loads a single patient and their graph from the DB by PatientID.

        Args:
            patient_uid (str): The PatientID to search for.

        Returns:
            Optional[Patient]: The Patient object if found, else None.
        """
        if self.db_path != ":memory:" and not os.path.exists(self.db_path):
            return None

        try:
            with self._get_connection() as conn:
                # conn.row_factory = sqlite3.Row
                cur = conn.cursor()

                # Fetch Patient
                p_row = cur.execute(
                    "SELECT * FROM patients WHERE patient_id = ?", (patient_uid,)).fetchone()
                if not p_row:
                    return None

                p = Patient(p_row['patient_id'], p_row['patient_name'])
                p_pk = p_row['id']
                stored_statuses = [(p, p_row['phi_status'])]
                # Collected during the walk and hydrated from the vertical
                # table in one pass afterwards. Unlike `load_all` this
                # filters by UID -- one patient's instances, not the whole
                # store's rows.
                hydrated_instances = []

                # Same one-query pre-fetch as load_all; see the note there.
                wave_refs = {
                    row['instance_uid']: row
                    for row in cur.execute(
                        "SELECT instance_uid, offset, length, compress_alg, hash"
                        " FROM instance_blobs WHERE kind = 'waveform'").fetchall()
                }

                # Fetch Studies
                st_rows = cur.execute(
                    "SELECT * FROM studies WHERE patient_id_fk = ?", (p_pk,)).fetchall()
                for st_r in st_rows:
                    st = Study(st_r['study_instance_uid'],
                               _as_loaded_date(st_r['study_date']))
                    # Same NULL-reads-False rule as load_all; see the
                    # note there. (#182)
                    st.date_shifted = bool(st_r['date_shifted'])
                    st_pk = st_r['id']
                    stored_statuses.append((st, st_r['phi_status']))

                    # Fetch Series
                    se_rows = cur.execute(
                        "SELECT * FROM series WHERE study_id_fk = ?", (st_pk,)).fetchall()
                    for se_r in se_rows:
                        se = Series(
                            se_r['series_instance_uid'],
                            se_r['modality'],
                            se_r['series_number'])
                        if se_r['manufacturer'] or se_r['model_name']:
                            se.equipment = Equipment(
                                se_r['manufacturer'], se_r['model_name'], se_r['device_serial_number'])
                        se_pk = se_r['id']

                        # Fetch Instances
                        i_rows = cur.execute(
                            "SELECT * FROM instances WHERE series_id_fk = ?", (se_pk,)).fetchall()
                        for r in i_rows:
                            inst = Instance(
                                r['sop_instance_uid'],
                                r['sop_class_uid'],
                                r['instance_number'],
                                file_path=r['file_path']
                            )
                            # See load_all: after construction, so the
                            # stored origin wins, and it is the only
                            # thing that restores it for a redacted
                            # instance whose `file_path` is NULL (#238).
                            if r['source_path']:
                                inst.source_path = r['source_path']
                            # Wire up Sidecar (Copy-Paste logic from load_all, keep generic?)
                            if r['attributes_json']:
                                try:
                                    attrs = json.loads(
                                        r['attributes_json'], object_hook=isocenter_json_object_hook)
                                    self._deserialize_into(inst, attrs)
                                except (json.JSONDecodeError, TypeError) as exc:
                                    # Silence here meant an instance loaded
                                    # with no attributes at all and nothing
                                    # anywhere said so.
                                    self.logger.error(
                                        "Could not decode stored attributes "
                                        "for instance %s: %s",
                                        r['sop_instance_uid'], exc)

                            # Wire up Sidecar. Duplicates load_all's loader
                            # construction below; the two have drifted apart
                            # before, so change them together.
                            if r['pixel_offset'] is not None and r['pixel_length'] is not None:
                                inst._pixel_loader = self._create_pixel_loader(
                                    r['pixel_offset'], r['pixel_length'], r['compress_alg'], inst)

                            self._wire_waveform_loader(
                                inst, wave_refs.get(r['sop_instance_uid']))

                            se.instances.append(inst)
                            hydrated_instances.append(inst)
                            stored_statuses.append((inst, r['phi_status']))

                        st.series.append(se)
                    p.studies.append(st)

                # Ahead of the status loop and of `mark_subtree_persisted`,
                # matching `load_all`. Not load-bearing on its own --
                # `_apply_vertical_attributes` advances no revision, so the
                # order is interchangeable today. It is kept because it is
                # what makes a later `set_attr` slipping in here survivable
                # rather than a silent UNSCANNED regression; the invariant
                # itself lives on that helper.
                vertical = self.load_vertical_attributes_bulk(
                    [i.sop_instance_uid for i in hydrated_instances], conn=conn)
                for inst in hydrated_instances:
                    self._apply_vertical_attributes(
                        inst, vertical.get(inst.sop_instance_uid, {}))

                for entity, stored in stored_statuses:
                    entity.record_phi_status(_phi_status_from_stored(stored))

                p.mark_subtree_persisted()
                return p
        except sqlite3.Error as e:
            self.logger.error(f"Failed to load patient: {e}")
            return None

    def _serialize_item(self, item: Instance) -> Dict[str, Any]:
        """
        Serializes a DicomItem (or Instance) to a dictionary, including attributes and sequences.
        """
        data = item.attributes.copy()
        if item.sequences:
            seq_data = {}
            for tag, seq in item.sequences.items():
                items_list = []
                for seq_item in seq.items:
                    # Recursive call for sequence items (which are DicomItems)
                    # We can reuse logic but need to handle DicomItem vs Instance
                    # Instance specific fields are handled by caller for the root,
                    # but for seq items they are just DicomItems.
                    items_list.append(self._serialize_dicom_item(seq_item))
                seq_data[tag] = items_list
            data['__sequences__'] = seq_data
        return data

    def _serialize_dicom_item(self, item) -> Dict[str, Any]:
        """Helper for recursive serialization of generic DicomItems."""
        data = item.attributes.copy()
        if item.sequences:
            seq_data = {}
            for tag, seq in item.sequences.items():
                items_list = [self._serialize_dicom_item(i) for i in seq.items]
                seq_data[tag] = items_list
            data['__sequences__'] = seq_data
        return data

    def _deserialize_into(self, target_item, data: Dict[str, Any]):
        """
        Populates target_item with attributes and sequences from data dict.
        """
        sequences_data = data.pop('__sequences__', None)

        # 1. Attributes
        target_item.attributes.update(data)

        # 2. Sequences
        if sequences_data:
            from .entities import DicomItem
            for tag, items_list in sequences_data.items():
                for item_data in items_list:
                    new_item = DicomItem()
                    self._deserialize_into(new_item, item_data)
                    target_item.add_sequence_item(tag, new_item)

    def save_vertical_attributes(
            self, instance_uid: str, attributes: Dict[Tuple[str, str], Any], conn: sqlite3.Connection = None):
        """
        Persists extended attributes to the vertical `instance_attributes` table.

        This handles private tags and attributes that don't fit in the core JSON.

        The write **replaces the instance's whole vertical set**: every row
        for `instance_uid` is deleted first, then the given attributes are
        inserted. An empty mapping therefore clears the instance, and is not
        a no-op. That is not tidiness -- it is the only shape that mirrors
        the read side. Deleting only the keys about to be re-inserted leaves
        a tag that was *removed* from the graph sitting in the table, and
        skipping the call when there is nothing to insert leaves the entire
        stripped block there. Both were invisible while nothing read the
        rows back; once `load_all` does (#158), either one puts a vendor
        block that `remove_private_tags=True` deleted back into a
        de-identified graph on the next reload.

        Args:
            instance_uid (str): The SOP Instance UID.
            attributes (Dict[Tuple[str, str], Any]): Mapping of (Group, Element) hex strings to values.
            conn (sqlite3.Connection, optional): An existing database connection to use for the transaction.
        """
        data_rows = []
        for (grp, elem), val in attributes.items():
            vr = "UN"  # Todo: Pass VR from caller
            # Check for VM > 1. `MultiValue` is what pydicom hands back for a
            # multi-valued element and it is a MutableSequence, NOT a list, so
            # a bare `isinstance(val, list)` sent it down the scalar arm and
            # stored "['a', 'b', 'c']" in one row -- a string that reloads
            # looking like a list. `IsocenterJSONEncoder` unwraps MultiValue
            # for the other tier for the same reason.
            if isinstance(val, (list, MultiValue)):
                for idx, atom in enumerate(val):
                    data_rows.append((instance_uid, grp, elem, idx, vr, str(atom)))
            else:
                data_rows.append((instance_uid, grp, elem, 0, vr, str(val)))

        try:

            # If conn is passed, use it (and don't close it/commit it here, leave to caller).
            # If not, create new context (which commits/closes).
            ctx = self._get_connection() if conn is None else nullcontext(conn)

            with ctx as db:
                # Delete-then-insert rather than UPSERT: an UPSERT keyed on
                # (uid, grp, elem, atom) leaves atoms 1 and 2 behind when a
                # VM 3 value shrinks to VM 1. The delete is by instance_uid
                # alone -- see the docstring for why a per-key delete is not
                # enough.
                db.execute(
                    "DELETE FROM instance_attributes WHERE instance_uid=?",
                    (instance_uid,))

                if data_rows:
                    db.executemany("""
                        INSERT INTO instance_attributes (instance_uid, group_id, element_id, atom_index, value_rep, value_text)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, data_rows)

        except sqlite3.Error as e:
            # Re-raised, and that matters more than it used to. The DELETE
            # above is de-identification work: it is what removes a private
            # tag the graph no longer has. Swallowing the error here would
            # let the caller's transaction commit the instance row and mark
            # the instance persisted, so a save that failed to strip the
            # vendor block would report success and never be retried. The
            # raise reaches `save_all`, which rolls back and leaves the
            # instances dirty (see
            # `test_a_failed_save_reports_the_error_that_caused_it`).
            self.logger.error(f"Failed to save vertical attributes for {instance_uid}: {e}")
            raise e

    def load_vertical_attributes(self, instance_uid: str) -> Dict[Tuple[str, str], Any]:
        """
        Loads extended attributes from vertical table.

        Args:
            instance_uid (str): The SOP Instance UID.

        Returns:
            Dict[Tuple[str, str], Any]: Dictionary mapping (group, element) tuples to values.
        """
        return self.load_vertical_attributes_bulk([instance_uid]).get(instance_uid, {})

    def reconcile_private_tags(self) -> Tuple[int, Dict[str, List[str]]]:
        """Drop `instance_attributes` rows absent from the core attributes.

        The database half of `DicomSession.reconcile_private_tags()`
        (#172), which carries the public contract and the warnings; use
        that. This method decides nothing -- it applies the one rule the
        caller opted into: the core `attributes_json` is read as the
        complete answer to "which tags does this instance have", and
        every tier row whose tag is not there is deleted.

        Why the core can be the answer for the store this exists for: a
        store written before #158 was read core-only -- nothing consulted
        the tier -- so the core IS what every pre-upgrade session saw,
        scanned and exported. For any other store the tier holds the
        instance's private text values *by design* and this deletes
        them, which is why nothing calls this automatically.

        A tier row whose instance is not in `instances` at all is
        dropped too: it can reach no graph from this store, and keeping
        it preserves exactly the kind of unreadable residue this call
        exists to clear.

        Returns:
            Tuple[int, Dict[str, List[str]]]: rows deleted (rows, not
            tags -- a VM=3 value is three rows), and per-instance
            `{sop_instance_uid: [tags]}` so the caller can heal the live
            graph and write the audit trail.
        """
        dropped: Dict[str, List[str]] = {}
        rows_deleted = 0
        with self._get_connection() as conn:
            core: Dict[str, set] = {}
            for row in conn.execute(
                    "SELECT sop_instance_uid, attributes_json FROM instances"):
                try:
                    attrs = json.loads(row['attributes_json'] or "{}")
                except (json.JSONDecodeError, TypeError):
                    attrs = {}
                core[row['sop_instance_uid']] = set(attrs.keys())

            stale = [
                (row['instance_uid'], row['group_id'], row['element_id'])
                for row in conn.execute(
                    "SELECT DISTINCT instance_uid, group_id, element_id"
                    " FROM instance_attributes")
                if (f"{row['group_id']},{row['element_id']}"
                    not in core.get(row['instance_uid'], set()))
            ]

            cur = conn.cursor()
            for uid, grp, elem in stale:
                cur.execute(
                    "DELETE FROM instance_attributes WHERE instance_uid=?"
                    " AND group_id=? AND element_id=?", (uid, grp, elem))
                rows_deleted += cur.rowcount
                dropped.setdefault(uid, []).append(f"{grp},{elem}")
            conn.commit()

        return rows_deleted, dropped

    def load_vertical_attributes_bulk(
            self,
            instance_uids: Optional[List[str]] = None,
            conn: sqlite3.Connection = None
    ) -> Dict[str, Dict[Tuple[str, str], Any]]:
        """Loads the vertical table for many instances in one pass.

        Hydration needs this tier for every instance it builds, and the
        whole point of the standard/private split is that loading 10k
        instances does not mean 10k queries. `load_vertical_attributes`
        takes a single UID, so calling it per instance would put exactly
        that shape on the default session-open path. This is the same move
        as the `wave_refs` pre-fetch in `load_all`: one query, stitched in
        memory.

        Values come back as they are stored -- `str`, or a `list` of `str`
        for VM > 1. **No type is restored**, because none is recorded:
        `value_rep` is hardcoded to "UN" on write and is not read here, so
        a private `5` reloads as `"5"`. Reconstructing a type by inspecting
        the text would be a storage-shape decision, and it is #154's, not
        this method's. For the same reason a saved one-element list reloads
        as a scalar: the table records no arity either.

        Args:
            instance_uids (Optional[List[str]]): SOP Instance UIDs to fetch.
                `None` means every row in the table, which is what a
                whole-store load wants. A list is chunked to stay under
                SQLite's bound-parameter limit.
            conn (sqlite3.Connection, optional): An existing connection to
                read on. Callers already inside a `_get_connection` block
                MUST pass theirs -- on a `:memory:` store `_memory_lock` is
                a plain, non-reentrant lock, so opening a nested connection
                deadlocks outright. Same convention as
                `save_vertical_attributes` and `record_blob_ref`.

        Returns:
            Dict[str, Dict[Tuple[str, str], Any]]: SOP Instance UID ->
            {(group, element): value}. Instances with no vertical rows are
            absent rather than present-and-empty.
        """
        select = ("SELECT instance_uid, group_id, element_id, value_text"
                  " FROM instance_attributes")
        # atom_index is not selected, only ordered by: it decides the order
        # of a multi-valued element's atoms and carries nothing else.
        order = " ORDER BY instance_uid, group_id, element_id, atom_index"

        if instance_uids is None:
            queries = [(select + order, ())]
        else:
            uids = list(dict.fromkeys(instance_uids))
            if not uids:
                return {}
            queries = []
            for start in range(0, len(uids), _VERTICAL_UID_CHUNK):
                chunk = uids[start:start + _VERTICAL_UID_CHUNK]
                placeholders = ",".join("?" * len(chunk))
                queries.append((
                    f"{select} WHERE instance_uid IN ({placeholders}){order}",
                    tuple(chunk)))

        # No `except sqlite3.Error: return {}` here, which is what the
        # per-UID version did. An empty result from this method is
        # indistinguishable from an instance that genuinely has no private
        # tags, so swallowing a read failure reproduces #158 exactly --
        # private tags absent from the graph, absent from the export, and
        # nothing saying so. `load_all` and `load_patient` have their own
        # handlers and turn a store-level failure into a logged empty
        # load, which is loud. Failing that way is the point.
        atoms: Dict[Tuple[str, str, str], List[str]] = {}
        ctx = self._get_connection() if conn is None else nullcontext(conn)
        with ctx as db:
            for sql, params in queries:
                # Iterated, not fetchall()'d: the rows are turned into the
                # grouped result as they arrive rather than held twice,
                # which matters when the filter is None and the table
                # covers the whole store.
                for row in db.execute(sql, params):
                    key = (row['instance_uid'], row['group_id'], row['element_id'])
                    atoms.setdefault(key, []).append(row['value_text'])

        results: Dict[str, Dict[Tuple[str, str], Any]] = {}
        for (uid, grp, elem), values in atoms.items():
            results.setdefault(uid, {})[(grp, elem)] = (
                values if len(values) > 1 else values[0])
        return results

    @staticmethod
    def _apply_vertical_attributes(instance: Instance,
                                   private: Dict[Tuple[str, str], Any]) -> None:
        """Writes loaded private tags onto an instance being hydrated.

        Assigns into `attributes` directly, exactly as `_deserialize_into`
        does, and never through `set_attr`. That is the invariant, and the
        reason is `phi_status`, not `has_unsaved_changes`: `set_attr`
        advances `_revision`, and a status recorded against a revision the
        entity has since left reads back as `UNSCANNED` by design. An
        instance rebuilt from a row that recorded a conclusion would then
        report that nothing is known about it.

        Both callers do apply these values before their
        `record_phi_status` loop and before `mark_subtree_persisted()`,
        which between them absorb a stray bump -- so a `set_attr` here
        would be survivable and, worse, invisible: it passes every
        round-trip test. The ordering is defence and worth keeping; direct
        assignment is the rule. Pinned by
        `test_applying_a_loaded_private_tag_is_not_an_edit`, which is the
        only test that fails when this line changes.
        """
        for (grp, elem), value in private.items():
            instance.attributes[f"{grp},{elem}"] = value

    def persist_blob(self, instance, kind: str, data) -> None:
        """Write a binary blob to the sidecar and record its reference.

        Args:
            instance (Instance): Owning instance.
            kind (str): 'pixels' or 'waveform'.
            data (bytes | np.ndarray): Payload. Arrays are passed to the
                sidecar directly to avoid a full copy.

        Raises:
            ValueError: If `kind` is not a recognised blob kind.
        """
        import hashlib

        if kind not in ("pixels", "waveform"):
            raise ValueError("Unknown blob kind: {!r}".format(kind))

        if data is None:
            return

        raw = data.tobytes() if hasattr(data, "tobytes") else data
        digest = hashlib.sha256(raw).hexdigest()

        c_alg = 'zlib'
        offset, length = self.sidecar.write_frame(data, c_alg)

        self.record_blob_ref(
            instance.sop_instance_uid, kind, offset, length, digest, c_alg)

        instance.mark_modified()

    def record_blob_ref(self, instance_uid: str, kind: str, offset: int,
                        length: int, blob_hash: str, compress_alg: str,
                        conn: sqlite3.Connection = None) -> None:
        """Record a sidecar reference without writing to the sidecar.

        The ingest path writes frames itself via SidecarManager, so it needs
        to register the resulting reference separately. Without this, the
        blob is invisible to `compact_sidecar` and would be reclaimed as
        dead space.

        Callers already inside a transaction MUST pass their connection.
        Opening a nested one is not merely untidy: on a file-backed DB the
        inner write blocks on the outer write lock for the full 900 s
        `timeout` (see `_get_connection`) before failing, and on a `:memory:`
        store `_memory_lock` is a plain, non-reentrant `threading.Lock`, so
        it deadlocks outright. Follows the same `conn=None` convention as
        `save_vertical_attributes`.

        Args:
            instance_uid (str): Owning SOP Instance UID.
            kind (str): 'pixels' or 'waveform'.
            offset (int): Byte offset of the blob within the sidecar.
            length (int): On-disk (post-compression) length in bytes.
            blob_hash (str): SHA-256 of the raw (uncompressed) payload.
            compress_alg (str): Compression used, e.g. 'zlib' or 'raw'.
            conn (sqlite3.Connection, optional): An existing connection to
                enlist in. When given, no new connection is opened and the
                write joins the caller's transaction.

        Raises:
            ValueError: If exactly one of `offset`/`length` is None. A
                half-specified reference is never recoverable: it would pair
                a real offset with a missing or stale length. Callers with
                nothing to record must skip the call, not pass NULLs.
        """
        if (offset is None) != (length is None):
            raise ValueError(
                "Blob reference for {!r}/{!r} must supply both offset and "
                "length, got offset={!r} length={!r}".format(
                    instance_uid, kind, offset, length))
        # offset/length/compress_alg describe ONE generation of the blob and
        # are assigned together -- COALESCE-ing any of them could pair a new
        # offset with a stale length or algorithm, which decodes garbage
        # rather than failing. `hash` is different: it is knowledge ABOUT the
        # payload, and a caller that does not happen to have it (a hydrated
        # instance re-saved after a tag edit carries no `_pixel_hash`) means
        # "unknown", not "none". Erasing a recorded hash there would leave
        # this row disagreeing with instances.pixel_hash, so it is COALESCEd,
        # matching the sibling `instances` upsert in save_all.
        sql = """
            INSERT INTO instance_blobs
                (instance_uid, kind, file_id, offset, length, hash, compress_alg)
            VALUES (?, ?, 0, ?, ?, ?, ?)
            ON CONFLICT(instance_uid, kind) DO UPDATE SET
                offset=excluded.offset,
                length=excluded.length,
                hash=COALESCE(excluded.hash, instance_blobs.hash),
                compress_alg=excluded.compress_alg
        """
        params = (instance_uid, kind, offset, length, blob_hash, compress_alg)

        if conn is not None:
            conn.execute(sql, params)
            return

        with self._get_connection() as own_conn:
            own_conn.execute(sql, params)

    def get_blob_ref(self, instance_uid: str, kind: str):
        """Return the sidecar reference for a blob, or None if absent.

        Args:
            instance_uid (str): Owning SOP Instance UID.
            kind (str): 'pixels' or 'waveform'.

        Returns:
            Optional[dict]: Keys `offset`, `length`, `hash`, `compress_alg`.
        """
        with self._get_connection() as conn:
            row = conn.execute("""
                SELECT offset, length, hash, compress_alg
                FROM instance_blobs
                WHERE instance_uid = ? AND kind = ?
            """, (instance_uid, kind)).fetchone()

        if row is None:
            return None
        return {
            "offset": row["offset"],
            "length": row["length"],
            "hash": row["hash"],
            "compress_alg": row["compress_alg"],
        }

    def get_blob_refs(self, kind: str) -> Dict[str, Tuple[int, int]]:
        """Return every sidecar reference of one kind, in a single query.

        `compact_sidecar` returns a pixels-only uid_map on purpose (keying by
        UID alone cannot distinguish kinds, and a waveform offset handed to a
        pixel loader decodes garbage). Callers that need to repoint non-pixel
        loaders after a compaction read the post-compaction truth from here
        instead.

        Args:
            kind (str): 'pixels' or 'waveform'.

        Returns:
            Dict[str, Tuple[int, int]]: instance_uid -> (offset, length), for
            rows that have both. Half-specified rows are impossible via
            `record_blob_ref`, but are skipped defensively rather than
            yielding a None-bearing pair.
        """
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT instance_uid, offset, length
                FROM instance_blobs
                WHERE kind = ? AND offset IS NOT NULL AND length IS NOT NULL
            """, (kind,)).fetchall()

        return {r["instance_uid"]: (r["offset"], r["length"]) for r in rows}

    def persist_pixel_data(self, instance: Instance):
        """
        Immediately persists pixel data to the sidecar to allow memory offloading.

        This writes the `pixel_array` to the sidecar file and updates the instance's
        `_pixel_loader` and `_pixel_hash`. It does NOT update the full instance record
        in the main DB, only the pixel linkage in memory (marked dirty).

        Args:
            instance (Instance): The instance containing the pixel data to persist.
        """
        try:
            # The read -> sidecar write -> loader/hash rebind must be one
            # critical section against `_persist_pixels`: a background
            # save that reads the bytes before this redaction swap zeroes
            # them, and rebinds after it, leaves the instance reading
            # back its pre-redaction pixels under a redaction attestation
            # (#274). Released before `record_blob_ref` below -- never
            # hold a thread lock across a sqlite write that can wait out
            # the busy timeout.
            with self._pixel_swap_lock:
                # 1. Write to Sidecar
                # Pass array directly to avoid .tobytes() Memory spike
                # (Zero-Copy 500MB save)
                # One read, inside the lock, and every branch below asks
                # this local. The None check used to sit above the `try`,
                # outside the lock, while the read that feeds the hash sat
                # here -- so an `unload_pixel_data()` landing between them
                # left `b_data` None, `hasattr(None, 'tobytes')` False, and
                # `hashlib.sha256(None)` raising `TypeError: object
                # supporting the buffer API required` into the redaction
                # swap (#288). Returning here skips `record_blob_ref` and
                # `mark_modified` exactly as the old early return did.
                b_data = instance.pixel_array
                if b_data is None:
                    return

                # Hash Update (CRITICAL for Integrity Checks)
                # Calculate Hash BEFORE writing/compression to ensure we
                # capture the state exactly as it goes into the pipe.
                import hashlib
                # Ensure we are hashing the contiguous bytes
                if hasattr(b_data, 'tobytes'):
                    p_hash = hashlib.sha256(b_data.tobytes()).hexdigest()
                else:
                    p_hash = hashlib.sha256(b_data).hexdigest()

                instance._pixel_hash = p_hash

                # Determine suitable compression? Defaulting to zlib for
                # swap. Ideally we respect original or config, but for
                # swap zlib is safe/fast enough.
                c_alg = 'zlib'

                offset, length = self.sidecar.write_frame(b_data, c_alg)

                # 2. Update Instance Loader
                # This allows instance.unload_pixel_data() to work safely
                # Note: instance attributes ARE populated here (it's a
                # live object), so passing instance=instance works.
                instance._pixel_loader = self._create_pixel_loader(
                    offset, length, c_alg, instance, pixel_hash=p_hash)
                # The loader now points at the bytes that are resident, so
                # the array is recoverable and freeable again (#293).
                instance._pixel_array_unwritten = False

            # 3. Optional: Persist the linkage to DB immediately?
            # It's safer if we do, so if we crash, we know where the pixels are.
            # However, if we don't save the attributes/UID changes, the DB is out of sync anyway.
            # But the primary goal here is MEMORY MANAGEMENT.
            # So updating the object state in memory (step 2) is sufficient for unload_pixel_data() to return True.
            # The final session.save() will record the new offset/length into the DB
            # instances table.

            # Mirror the reference into the kind-keyed blob table so
            # compaction and waveform storage share one index.
            self.record_blob_ref(
                instance.sop_instance_uid, 'pixels', offset, length, p_hash, c_alg)

            # The instance must be marked modified so save_all writes the
            # new loader and hash. Without this an otherwise-unchanged
            # instance is skipped, leaving the database pointing at the
            # original data while memory points at the new sidecar frame.
            instance.mark_modified()

        except Exception as e:
            self.logger.error(f"Failed to persist pixel swap for {instance.sop_instance_uid}: {e}")
            raise e

    def save_all(self, patients: List[Patient],
                 prune_absent_patients: bool = False):
        """
        Incrementally persists the provided patients and their graph.

        Walks Patient -> Study -> Series -> Instance, upserting anything
        that has unsaved changes, and deleting instances that are in the
        database but
        no longer in memory. The whole walk runs in one transaction, so a
        failure anywhere leaves the database exactly as it was found.

        Args:
            patients (List[Patient]): The patient objects to save.
            prune_absent_patients (bool): Delete patient rows that `patients`
                does not contain. Only correct when the list is the entire
                contents of the session, so it defaults to off: a partial
                save that pruned would turn "store this one patient" into
                "delete everyone else". `DicomSession.save()` owns the whole
                store and passes True, which is what stops an anonymised
                patient's original row surviving under its old identifier.
        """
        self.logger.info(
            "Saving %d patients to %s (Incremental)...", len(patients), self.db_path)

        tally = _SaveTally()
        # Instances are marked clean only after the commit returns. Doing it
        # inside the walk -- as this method used to -- means a rolled-back
        # save leaves memory believing it was written, so the retry skips
        # exactly the rows that failed. These are references to objects that
        # are already resident, so holding them costs a pointer each.
        saved_instances = []

        # Every sidecar frame this save will need is appended HERE, before
        # any connection exists. It used to happen inside the walk below,
        # which meant the SQLite write lock was held for as long as the
        # save's whole dirty resident pixel payload took to compress and
        # write -- so a slow-storage save could outlast
        # `_SQLITE_BUSY_TIMEOUT_S` and surface in a healthy concurrent
        # writer as `database is locked` (#287). The transaction below now
        # contains row upserts and nothing else.
        prepared = self._prepare_pixel_frames(patients, tally)

        try:
            with self._get_connection() as conn:
                cur = conn.cursor()
                pending_deletions = []
                for patient in patients:
                    saved_instances.extend(
                        self._save_patient(conn, cur, patient, tally,
                                           pending_deletions, prepared))

                # Every upsert in the whole save has now run, so a child
                # that moved to a different parent already points at it and
                # a scoped delete will not mistake it for a removal (#77).
                for delete, parent, parent_pk in pending_deletions:
                    delete(cur, parent, parent_pk)

                if prune_absent_patients:
                    self._delete_absent_patients(cur, patients)

                conn.commit()
        except Exception:
            # No rollback here: `_get_connection` owns the transaction and
            # has already rolled it back and closed the connection by the
            # time this runs. Calling `conn.rollback()` on the closed
            # handle raised `ProgrammingError: Cannot operate on a closed
            # database`, which then replaced the real exception -- every
            # distinct save failure surfaced under one misleading name.
            self.logger.error(
                "Save failed; the transaction was rolled back and nothing was "
                "marked clean", exc_info=True)
            raise

        for instance, revision in saved_instances:
            instance.mark_persisted(revision)

        self._log_save_summary(tally)

    def _save_patient(self, conn, cur, patient, tally,
                      pending_deletions, prepared) -> List[Tuple[Instance, int]]:
        """Persists one patient's subtree. Returns the instances written.

        Deletions are appended to `pending_deletions` rather than run
        here. They must not execute until every parent in the save has
        been walked: a scoped `WHERE parent_id_fk = ?` delete is only
        correct once the row it might delete has had the chance to be
        claimed by its new parent (#77).
        """
        patient_pk = self._upsert_patient(cur, patient, tally)
        if patient_pk is None:
            return []

        saved = []
        for study in patient.studies:
            study_pk = self._upsert_study(cur, study, patient_pk, tally)
            if study_pk is None:
                continue

            # A child moved between parents mutates the *parent's* list,
            # which marks nothing dirty -- so the child's own upsert never
            # runs and its foreign key would still name the old parent.
            # Correcting it here, from the parent that now holds it, is
            # what makes the deferred deletion below see the truth (#77).
            self._reparent_series(cur, study, study_pk)

            for series in study.series:
                series_pk = self._upsert_series(cur, series, study_pk, tally)
                if series_pk is None:
                    continue
                self._reparent_instances(cur, series, series_pk)
                pending_deletions.append(
                    (self._delete_removed_instances, series, series_pk))
                saved.extend(
                    self._save_unsaved_instances(
                        conn, cur, series, series_pk, tally, prepared))

            pending_deletions.append(
                (self._delete_removed_series, study, study_pk))

        pending_deletions.append(
            (self._delete_removed_studies, patient, patient_pk))
        return saved

    def _upsert_patient(self, cur, patient, tally) -> Optional[int]:
        """Writes the patient row if dirty; returns its primary key."""
        if patient.has_unsaved_changes:
            cur.execute("""
                INSERT INTO patients (patient_id, patient_name, phi_status)
                VALUES (?, ?, ?)
                ON CONFLICT(patient_id) DO UPDATE SET
                    patient_name=excluded.patient_name,
                    phi_status=excluded.phi_status
            """, (patient.patient_id, patient.patient_name,
                  patient.phi_status.value))
            tally.patients += 1

        # Re-read rather than use lastrowid: the row may have existed
        # already, in which case the UPSERT updated it and no id was
        # allocated. Children need the real key either way.
        row = cur.execute(
            "SELECT id FROM patients WHERE patient_id=?",
            (patient.patient_id,)).fetchone()
        return row[0] if row else None

    def _upsert_study(self, cur, study, patient_pk, tally) -> Optional[int]:
        """Writes the study row if dirty; returns its primary key."""
        if study.has_unsaved_changes:
            cur.execute("""
                INSERT INTO studies (patient_id_fk, study_instance_uid, study_date, date_shifted, phi_status)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(study_instance_uid) DO UPDATE SET
                    study_date=excluded.study_date,
                    date_shifted=excluded.date_shifted,
                    patient_id_fk=excluded.patient_id_fk,
                    phi_status=excluded.phi_status
            """, (patient_pk, study.study_instance_uid,
                  _as_stored_date(study.study_date),
                  1 if study.date_shifted else 0,
                  study.phi_status.value))
            tally.studies += 1

        row = cur.execute(
            "SELECT id FROM studies WHERE study_instance_uid=?",
            (study.study_instance_uid,)).fetchone()
        return row[0] if row else None

    def _upsert_series(self, cur, series, study_pk, tally) -> Optional[int]:
        """Writes the series row if dirty; returns its primary key."""
        if series.has_unsaved_changes:
            equipment = series.equipment
            cur.execute("""
                INSERT INTO series (study_id_fk, series_instance_uid, modality, series_number, manufacturer, model_name, device_serial_number)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(series_instance_uid) DO UPDATE SET
                    modality=excluded.modality,
                    series_number=excluded.series_number,
                    manufacturer=excluded.manufacturer,
                    model_name=excluded.model_name,
                    device_serial_number=excluded.device_serial_number,
                    study_id_fk=excluded.study_id_fk
            """, (study_pk, series.series_instance_uid, series.modality,
                  series.series_number,
                  equipment.manufacturer if equipment else "",
                  equipment.model_name if equipment else "",
                  equipment.device_serial_number if equipment else ""))
            tally.series += 1

        row = cur.execute(
            "SELECT id FROM series WHERE series_instance_uid=?",
            (series.series_instance_uid,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def _reparent_series(cur, study, study_pk) -> None:
        """Point this study's series rows at it, wherever they were before.

        `_upsert_series` writes only when the series has unsaved changes,
        and moving a series between studies mutates the old study's list
        rather than the series -- so nothing marks it dirty and its
        `study_id_fk` would go on naming the study that no longer holds
        it. Restricted to rows whose key actually differs, so the ordinary
        case costs one statement and updates nothing.
        """
        uids = [s.series_instance_uid for s in study.series]
        if not uids:
            return
        placeholders = ",".join("?" * len(uids))
        cur.execute(
            f"UPDATE series SET study_id_fk=? "
            f"WHERE series_instance_uid IN ({placeholders}) AND study_id_fk!=?",
            (study_pk, *uids, study_pk))

    @staticmethod
    def _reparent_instances(cur, series, series_pk) -> None:
        """Point this series' instance rows at it. See `_reparent_series`."""
        uids = [i.sop_instance_uid for i in series.instances]
        if not uids:
            return
        placeholders = ",".join("?" * len(uids))
        cur.execute(
            f"UPDATE instances SET series_id_fk=? "
            f"WHERE sop_instance_uid IN ({placeholders}) AND series_id_fk!=?",
            (series_pk, *uids, series_pk))

    @staticmethod
    def _delete_removed_instances(cur, series, series_pk) -> int:
        """Deletes rows for instances no longer present in memory.

        Run for every series, changed or not. Removing an instance from a
        series' list mutates a plain Python list, which marks nothing --
        so the only way to notice a deletion is to compare the two sets.
        """
        stored = {row[0] for row in cur.execute(
            "SELECT sop_instance_uid FROM instances WHERE series_id_fk=?",
            (series_pk,)).fetchall()}
        removed = stored - {i.sop_instance_uid for i in series.instances}
        _delete_instances(cur, removed)
        return len(removed)

    @staticmethod
    def _delete_removed_series(cur, study, study_pk) -> int:
        """Deletes series no longer present in memory, and their instances."""
        stored = {row[1]: row[0] for row in cur.execute(
            "SELECT id, series_instance_uid FROM series WHERE study_id_fk=?",
            (study_pk,)).fetchall()}
        in_memory = {s.series_instance_uid for s in study.series}
        removed = [pk for uid, pk in stored.items() if uid not in in_memory]
        _delete_series_subtrees(cur, removed)
        return len(removed)

    @staticmethod
    def _delete_removed_studies(cur, patient, patient_pk) -> int:
        """Deletes studies no longer present in memory, and their subtrees."""
        stored = {row[1]: row[0] for row in cur.execute(
            "SELECT id, study_instance_uid FROM studies WHERE patient_id_fk=?",
            (patient_pk,)).fetchall()}
        in_memory = {s.study_instance_uid for s in patient.studies}
        removed = [pk for uid, pk in stored.items() if uid not in in_memory]
        _delete_study_subtrees(cur, removed)
        return len(removed)

    @staticmethod
    def _delete_absent_patients(cur, patients) -> int:
        """Deletes patient rows the in-memory store no longer contains.

        This is what closes the gap anonymisation opens. Patients are
        upserted on `patient_id`, so changing that value -- exactly what
        de-identification does -- writes a *new* row and orphans the old
        one, with the original name and identifier still in it. The
        studies are re-parented to the new row, so nothing ever visits the
        old one again and no scoped deletion reaches it.

        Runs after every patient has been written, never before: the
        re-parenting has to have happened already, or this would delete
        the subtree the new row is about to adopt.
        """
        stored = {row[1]: row[0] for row in cur.execute(
            "SELECT id, patient_id FROM patients").fetchall()}
        in_memory = {p.patient_id for p in patients}
        removed = [pk for pid, pk in stored.items() if pid not in in_memory]
        _delete_patient_subtrees(cur, removed)
        return len(removed)

    def _save_unsaved_instances(self, conn, cur, series, series_pk,
                              tally, prepared) -> List[Tuple[Instance, int]]:
        """Upserts the instances of one series that have unsaved changes.

        Returns the (instance, revision) pairs written, for the caller to
        mark persisted once the transaction has actually committed. The
        revision is captured *before* the write, so a concurrent edit
        arriving mid-save is not mistaken for the state that was stored.

        Neither the dirty set nor the revision is computed here any more:
        both were fixed by `_prepare_pixel_frames` before the transaction
        opened (#287), and this method selects the instances that prepass
        claimed. The set is therefore FROZEN at prepass time -- an
        instance dirtied afterwards is simply not saved this round and
        stays dirty for the next, which is the same direction of error
        `mark_persisted`'s capture-before-write discipline already takes.

        The selection is by `id(inst)`, never by SOP Instance UID. A UID
        can change in place between the prepass and here (redaction does
        exactly that), and a UID-keyed miss does not merely skip the
        write: `_delete_removed_instances` below then sees the instance's
        OLD row as orphaned and deletes it, so the instance vanishes from
        the index with nothing to put it back.

        The row tuple and `vertical_rows` deliberately read the LIVE
        `inst.sop_instance_uid` rather than anything the prepass
        captured, so a renamed instance is inserted under its new name
        and its old row is correctly reaped as removed.
        """
        unsaved = [(inst, prepared[id(inst)][0])
                   for inst in series.instances
                   if id(inst) in prepared]
        if not unsaved:
            return []

        rows, blob_rows, vertical_rows = self._build_instance_writes(
            unsaved, series_pk, tally, prepared)

        cur.executemany(_UPSERT_INSTANCE_SQL, rows)

        # Routed through record_blob_ref rather than inlined, so that exactly
        # one place knows how an instance_blobs row is written. Duplicating
        # the SQL here is what let this mirror's conflict clause drift away
        # from its sibling. `conn=conn` keeps it in this transaction.
        for blob_row in blob_rows:
            self.record_blob_ref(*blob_row, conn=conn)

        # Deferred until the instances exist: instance_attributes has a
        # foreign key onto instances(sop_instance_uid).
        for uid, attributes in vertical_rows:
            self.save_vertical_attributes(uid, attributes, conn=conn)

        tally.instances += len(unsaved)
        return unsaved

    def _build_instance_writes(self, unsaved, series_pk, tally, prepared):
        """Turns (instance, revision) pairs into the rows three tables need.

        This method does **no I/O of any kind**. Its predecessor wrote
        each instance's pixel frame to the sidecar from right here, which
        put bulk I/O inside `save_all`'s open transaction; the frames now
        arrive already written, in `prepared`, from the prepass that runs
        before the connection opens (#287). Nothing here touches the
        database either: the caller decides when, and in what order,
        these rows go in.

        `unsaved` carries the revision the prepass captured before the
        sidecar write started -- the same capture `mark_persisted` will
        be given -- so the #274 guard can be re-evaluated at the last
        moment, below, against a window that is now as long as all of the
        save's pixel I/O.

        Returns:
            Tuple of (instance rows, instance_blobs rows, (uid, private
            attributes) pairs for the vertical table).
        """
        rows, blob_rows, vertical_rows = [], [], []

        for inst, revision in unsaved:
            core, private = _split_core_and_private(self._serialize_item(inst))
            # Appended even when `private` is empty. An instance whose
            # private tags were all stripped still has to reach
            # `save_vertical_attributes`, or its old rows stay in the table
            # and the next reload puts them back on the graph (#158).
            vertical_rows.append((inst.sop_instance_uid, private))

            # The #274 guard, re-checked at the latest possible moment.
            # `_persist_pixels` applied it when it appended the frame, but
            # the prepass now runs before the transaction opens, so the
            # capture -> commit window spans every sidecar write in the
            # save. A redaction landing inside it rebinds the loader to
            # the redacted frame while `prepared` still holds the pristine
            # one; committing that row would leave `instance_blobs`
            # lagging `instances`, and the next compaction would copy the
            # stale frame forward and discard the redacted one -- the
            # resurrection the comment below warns about. Same semantics
            # as `_persist_pixels`'s own guard, later checkpoint: the
            # all-None frame, so the upsert's COALESCE leaves the stored
            # reference alone and the instance stays dirty against the
            # stale capture. The instance's row is still written -- NOT
            # dropped -- because that is what a raced instance got before
            # this restructure, and #287 is a restructure.
            frame = prepared[id(inst)][1]
            if inst._revision != revision:
                frame = _StoredFrame(None, None, None, None)
            rows.append((
                series_pk, inst.sop_instance_uid, inst.sop_class_uid,
                # Positional, and nothing checks this tuple against
                # `_UPSERT_INSTANCE_SQL`'s column list: `source_path`
                # sits immediately after `file_path` in both, and an
                # insertion in one and not the other writes the sidecar
                # offset into the path column without raising (#238).
                inst.instance_number, inst.file_path, inst.source_path,
                frame.offset, frame.length, frame.hash, frame.alg,
                json.dumps(core, cls=IsocenterJSONEncoder),
                # The property, not the stored field: an entity edited since
                # the scan reports UNSCANNED, and that is what belongs in the
                # row, whose attributes are the edited ones.
                inst.phi_status.value))

            # instance_blobs is what compaction reads, so it must never lag
            # behind `instances`. If it did, compaction would copy the STALE
            # frame forward and discard the current one -- silently
            # resurrecting pre-redaction pixels. Skipping NULL offsets
            # mirrors the COALESCE(...) in the upsert: "no new frame" must
            # leave the stored reference alone, not clear it.
            if frame.offset is not None and frame.length is not None:
                blob_rows.append((
                    inst.sop_instance_uid, 'pixels', frame.offset,
                    frame.length, frame.hash, frame.alg))

        return rows, blob_rows, vertical_rows

    def _prepare_pixel_frames(
            self, patients, tally) -> Dict[int, Tuple[int, '_StoredFrame']]:
        """Appends every dirty instance's pixel frame, before any transaction.

        Runs the whole Patient -> Study -> Series -> Instance walk once,
        ahead of `save_all`'s connection, and for each instance with
        unsaved changes captures its revision and calls `_persist_pixels`.
        The transaction that follows therefore does row upserts and
        nothing else -- no compression, no sidecar append, no `flock`
        wait -- so the SQLite write lock is no longer held for the length
        of the save's pixel payload (#287).

        Keyed by **object identity** (`id(inst)`), which is the only key
        that survives everything that can happen between this walk and
        the transaction's. Position cannot: `series_pk` is unknowable
        before the transaction and a series can be re-parented in
        between -- `_reparent_series` exists precisely because that
        happens. The SOP Instance UID cannot either, and that is the
        sharper trap, because it looks like the natural key: redaction
        mutates `sop_instance_uid` **in place** (`regenerate_uid()`,
        `Session._apply_redaction_outcomes`), so a UID-keyed lookup
        misses a renamed instance -- which then gets skipped by the walk
        while `_delete_removed_instances` deletes its old row as
        orphaned, losing the instance from the index entirely.

        `id()` rather than the instance itself as a dict key. This used
        to be forced -- `Instance` carried the dataclass default
        `eq=True`, making it unhashable and value-comparing -- but since
        #299 the entity classes are `eq=False`, so they hash and compare
        by identity and `prepared[inst]` WOULD now work. It is kept
        anyway: only the third of the three arguments above was ever
        about hashability, and the other two (position is unknowable
        before the transaction, and the SOP Instance UID is mutated in
        place) stand on their own, so re-spelling this map is a separate
        change to a load-bearing data-loss fix rather than a cleanup that
        #299 licenses. Filed as #300. `id()` is safe here for the
        usual reason it usually is not: `patients` holds the entire graph
        alive for the whole of `save_all`, and this map does not outlive
        that call, so no id can be recycled underneath it.

        Two consequences, both stated rather than hidden:

        - The dirty SET is frozen here. An instance dirtied after this
          walk is not saved this round; it stays dirty for the next.
        - The revision capture moves EARLIER, so the capture -> commit
          window grows by the length of the whole sidecar write. That is
          the safe direction: `mark_persisted` receives an older
          revision, so anything changing in the window leaves the
          instance dirty rather than falsely clean.

        A frame appended here whose transaction then rolls back is
        referenced by nothing. The sidecar is append-only and
        `compact_sidecar` rewrites only frames `instance_blobs` names, so
        the orphan is reclaimable dead space -- bounded by one save's
        dirty resident pixel bytes. Bounded and reclaimable ON DEMAND, not
        self-healing: `session.compact()` is manual and nothing reclaims
        automatically. Same artifact class the #274 revision guard
        already produces.

        Returns:
            Dict[int, Tuple[int, _StoredFrame]]: id(instance) ->
            (revision captured before the write, the frame written).
        """
        prepared: Dict[int, Tuple[int, '_StoredFrame']] = {}
        for patient in patients:
            for study in patient.studies:
                for series in study.series:
                    for inst in series.instances:
                        if not inst.has_unsaved_changes:
                            continue
                        revision = inst._revision
                        frame = self._persist_pixels(
                            inst, tally, revision=revision)
                        prepared[id(inst)] = (revision, frame)
        return prepared

    def _persist_pixels(self, inst, tally, revision=None) -> '_StoredFrame':
        """Writes this instance's pixels to the sidecar if they are new.

        Three cases: pixels resident in memory (hash them, and write only
        if the bytes actually changed), pixels already swapped out to the
        sidecar (keep the reference the loader holds), or no pixels at all.

        `revision` is the caller's capture from before the save started.
        This runs on the persistence manager's thread against live
        objects a redaction pass may be mutating, so publishing what was
        read here needs two protections (#274):

        - The lock makes read -> write -> rebind atomic against
          `persist_pixel_data`, so this save's rebind can never land
          *after* a redaction's and rewire the instance to the stale
          frame.
        - The revision guard, checked at assignment time inside the lock
          (outside it, the check would be decorative), skips the rebind
          and the row when the instance changed after the capture: the
          same capture-before-write discipline as `mark_persisted`. The
          all-None frame means "leave the stored reference alone" via
          the upsert's COALESCE, the instance stays dirty, and the next
          save writes the truth.
        """
        # Lock order: `_pixel_swap_lock` before `sidecar._lock` (inside
        # `write_frame`), never reversed. No sqlite work happens in here;
        # the caller writes the rows later, off this lock.
        #
        # The lock must open *before* the first read of `pixel_array`,
        # not after it. `Instance.unload_pixel_data()` nulls that field
        # under no lock at all -- from `release_memory()` sweeps and from
        # redaction paths' `finally` -- so asking "is it None?" outside
        # and re-reading it inside is a TOCTOU window: the null landed
        # between the two and `.tobytes()` raised `AttributeError:
        # 'NoneType' object has no attribute 'tobytes'` inside
        # `save_all`'s open transaction, rolling the entire save back
        # (#288). One read, one local, every branch below asks the local.
        # The local is also what makes the write correct rather than
        # merely non-crashing: unload drops only the instance's
        # reference, numpy keeps the buffer alive for ours, and unload
        # never mutates contents -- so bytes written from `arr` are still
        # the instance's true pixels. Every instance now takes this lock,
        # including non-resident ones that used to return before it; a
        # few hundred nanoseconds each, uncontended.
        with self._pixel_swap_lock:
            arr = inst.pixel_array
            loader = inst._pixel_loader

            if arr is None:
                # Recording the loader's own frame is correct here BECAUSE
                # the precondition is now enforced upstream. This arm used
                # to be reachable with a *diverged* array: a
                # `set_pixel_data()` after a save left the loader pointing
                # at the superseded frame, `unload_pixel_data()` cleared
                # the new pixels anyway, and this arm then re-recorded the
                # old offset/length/hash and the save marked the instance
                # persisted -- store, sidecar, memory and `_pixel_hash` all
                # agreeing on the wrong frame, with every integrity check
                # passing. `unload_pixel_data()` now refuses to null a
                # diverged array (#293), so an `arr is None` here means the
                # array really was equal to what the loader points at.
                # Do not relax that refusal without revisiting this arm.
                if isinstance(loader, SidecarPixelLoader):
                    return _StoredFrame(loader.offset, loader.length,
                                        loader.alg,
                                        getattr(inst, '_pixel_hash', None))
                return _StoredFrame(None, None, None, None)

            raw = arr.tobytes()
            digest = hashlib.sha256(raw).hexdigest()

            # Deduplication: identical bytes already in the sidecar.
            # Appending them again would grow the file by a full frame
            # per save.
            if (getattr(inst, '_pixel_hash', None) == digest
                    and isinstance(loader, SidecarPixelLoader)):
                inst._pixel_hash = digest
                # These exact bytes are already in the sidecar and the
                # loader already points at them, so the resident array
                # is recoverable and freeable again (#293).
                inst._pixel_array_unwritten = False
                return _StoredFrame(loader.offset, loader.length,
                                    loader.alg, digest)

            offset, length = self.sidecar.write_frame(raw, _PIXEL_COMPRESSION)
            tally.pixel_bytes += length
            tally.pixel_frames += 1

            if revision is not None and inst._revision != revision:
                # The bytes read above no longer describe the instance:
                # a mutation (a redaction, most importantly) landed after
                # the caller's capture. Publishing them would write a row
                # and a loader for state the graph has already left --
                # exactly #274's poisoning. The frame already appended is
                # a harmless orphan; the instance is still dirty against
                # the captured revision, so the next save corrects the row.
                return _StoredFrame(None, None, None, None)

            # Re-point the loader so the array can be unloaded safely
            # later. `pixel_hash=digest` is passed explicitly, the way
            # `persist_pixel_data` does: left to default, the loader falls
            # back to `inst._pixel_hash`, which at this point is still the
            # digest of the frame these bytes just replaced -- so the next
            # read after an unload raised an integrity mismatch against
            # correctly-saved data (#212). Passing it removes the ordering
            # dependency between this call and the assignment below.
            inst._pixel_loader = self._create_pixel_loader(
                offset, length, _PIXEL_COMPRESSION, inst, pixel_hash=digest)
            inst._pixel_hash = digest
            # Published: the loader now points at these bytes, so the
            # resident array is recoverable and freeable. This clear is
            # what keeps `release_memory()` working after a
            # `set_pixel_data()`; miss it and every replaced-and-saved
            # instance becomes permanently unfreeable, silently, because
            # the sweep only logs counts (#293).
            inst._pixel_array_unwritten = False
            return _StoredFrame(offset, length, _PIXEL_COMPRESSION, digest)

    def _log_save_summary(self, tally) -> None:
        """One line describing what the save actually wrote."""
        if tally.patients + tally.instances <= 0:
            return

        message = (f"Save (Inc) complete. P:{tally.patients} St:{tally.studies} "
                   f"Se:{tally.series} I:{tally.instances}.")
        if tally.pixel_frames > 0:
            megabytes = tally.pixel_bytes / (1024 * 1024)
            message += (f" Sidecar: {tally.pixel_frames} frames "
                        f"({megabytes:.2f} MB).")
        self.logger.info(message)

    def get_total_instances(self) -> int:
        """
        Returns the total number of instances currently persisted.

        Returns:
            int: The count of rows in the instances table.
        """
        try:
            with self._get_connection() as conn:
                cur = conn.cursor()
                row = cur.execute("SELECT COUNT(*) FROM instances").fetchone()
                return row[0] if row else 0
        except sqlite3.Error as e:
            self.logger.error(f"Failed to count instances: {e}")
            return 0

    def get_flattened_instances(self,
                                patient_ids: List[str] = None,
                                instance_uids: List[str] = None,
                                page_size: int = FLATTENED_PAGE_SIZE):
        """
        Yields a flat dictionary for every instance in the DB.

        Useful for streaming exports or analysis without loading the entire graph into RAM.

        The rows come back one page at a time, and **no database handle is
        held between pages** (#164). That is not an optimisation; it is
        the only shape that lets this method do what it advertises. It
        used to `yield` from inside `with self._get_connection()`, which
        on a `:memory:` store holds `_memory_lock` -- a plain,
        non-reentrant lock -- across its own yield. A generator parked
        between rows therefore held the store's only lock and every other
        call on it blocked forever. Streaming *is* partial consumption,
        so the advertised usage was the one that hung; the two callers
        that worked did `list(...)` first, which defeats the purpose. On
        a file store nothing deadlocked, but the parked generator kept a
        connection and a live read snapshot open, which stops WAL
        checkpointing and lets the `-wal` file grow unbounded.

        Two consequences worth knowing before you rely on this:

        - **Iteration is not one snapshot.** Each page is its own query,
          so writes that land between pages are visible and rows deleted
          between pages are not returned. The previous single-cursor
          version was a single snapshot; that guarantee is gone, and it
          could not be kept without holding a read open across the yield,
          which is the defect.
        - **Order is by `instances.id`.** The walk is a keyset on that
          column, so the sequence is now defined rather than whatever the
          join happened to produce.

        Args:
            patient_ids (List[str], optional): Filter by list of Patient IDs.
            instance_uids (List[str], optional): Filter by list of SOP Instance UIDs.
            page_size (int, optional): Rows per page. Trades resident
                memory against the number of queries; see
                `FLATTENED_PAGE_SIZE`. Must be >= 1 -- `LIMIT 0` returns
                an empty page, and an empty page is how the walk decides
                it has finished, so a zero would silently report an empty
                store.

        Yields:
            dict: Flattend dictionary representing row data (patient, study, series, instance paths).

        Raises:
            ValueError: If `page_size` is below 1. This is a plain method
                wrapping a generator precisely so the check fires at the
                call, not on the first `next()`.
        """
        if page_size < 1:
            raise ValueError(
                f"page_size must be >= 1, got {page_size}")
        return self._iter_flattened_instances(
            patient_ids, instance_uids, page_size)

    def _iter_flattened_instances(self, patient_ids, instance_uids, page_size):
        """Keyset walk backing `get_flattened_instances`.

        Each page opens its own `_get_connection`, so the lock (or the
        connection, on a file store) is held for the query and nothing
        else. Paging by re-query rather than by `fetchmany` on a live
        cursor is not a preference: on the file path `_get_connection`
        **closes** the connection when its block exits, so a cursor
        cannot survive to a second page at all.

        The keyset is `instances.id`, which is an INTEGER PRIMARY KEY and
        therefore the rowid, so resuming is a seek rather than an OFFSET
        scan. It is selected as the first column and stripped back off
        before yielding -- it is a walk cursor, not part of the published
        row shape.
        """
        # `i.id` leads the select list so the keyset column has a fixed
        # position to slice off, whatever the rest of the list becomes.
        base_query = """
            SELECT
                i.id,
                p.patient_id, p.patient_name,
                st.study_instance_uid, st.study_date,
                s.series_instance_uid, s.modality, s.series_number, s.manufacturer, s.model_name, s.device_serial_number,
                i.sop_instance_uid, i.sop_class_uid, i.instance_number, i.file_path,
                i.pixel_offset, i.pixel_length, i.compress_alg, i.attributes_json
            FROM instances i
            JOIN series s ON i.series_id_fk = s.id
            JOIN studies st ON s.study_id_fk = st.id
            JOIN patients p ON st.patient_id_fk = p.id
        """

        filters = []
        filter_params = []

        if patient_ids:
            placeholders = ",".join("?" for _ in patient_ids)
            filters.append(f"p.patient_id IN ({placeholders})")
            filter_params.extend(patient_ids)

        if instance_uids:
            placeholders = ",".join("?" for _ in instance_uids)
            filters.append(f"i.sop_instance_uid IN ({placeholders})")
            filter_params.extend(instance_uids)

        after_id = 0
        while True:
            # Rebuilt per page: the keyset condition is appended last, so
            # its bound value must follow the filters' in `params` too.
            # A mismatch here binds the wrong value to the wrong
            # placeholder and returns wrong rows without raising.
            conditions = filters + ["i.id > ?"]
            params = list(filter_params) + [after_id]
            query = (base_query + " WHERE " + " AND ".join(conditions)
                     + " ORDER BY i.id LIMIT ?")
            params.append(page_size)

            with self._get_connection() as conn:
                cursor = conn.cursor().execute(query, params)
                cols = [desc[0] for desc in cursor.description][1:]
                page = cursor.fetchall()

            for row in page:
                after_id = row[0]
                yield dict(zip(cols, row[1:]))

            # A short page means `LIMIT` never filled, which only happens
            # when the scan reached the end. Rows the filter excluded do
            # not shorten a page -- `LIMIT` counts matches -- so this
            # cannot stop early on a sparse cohort.
            if len(page) < page_size:
                return

    def update_attributes(self, instances: List[Patient]):
        """
        Efficiently updates the attributes_json for a list of instances.

        Used when only attributes have changed (e.g., after locking identities)
        to avoid full graph traversal.

        Args:
            instances (List[Instance]): The list of instances to update.
        """
        if not instances:
            return

        self.logger.info(f"Updating attributes for {len(instances)} instances...")
        try:
            with self._get_connection() as conn:
                cur = conn.cursor()

                # Pre-calculate data for executemany
                data = []
                for inst in instances:
                    # Serialize attributes AND sequences
                    full_data = self._serialize_item(inst)
                    attrs_json = json.dumps(full_data, cls=IsocenterJSONEncoder)
                    data.append((attrs_json, inst.sop_instance_uid))

                cur.executemany("""
                    UPDATE instances
                    SET attributes_json = ?
                    WHERE sop_instance_uid = ?
                """, data)

                conn.commit()
                self.logger.info("Update complete.")

        except sqlite3.Error as e:
            self.logger.error(f"Failed to update attributes: {e}")

    def save_findings(self, findings: List[PhiFinding]):
        """
        Persists PHI findings to the database.

        Args:
            findings (List[PhiFinding]): List of finding objects to insert.
        """
        timestamp = datetime.now().isoformat()

        if not findings:
            return

        self.logger.info(f"Saving {len(findings)} PHI findings...")

        try:
            with self._get_connection() as conn:
                cur = conn.cursor()

                # Prepare Data Generator for Batch Insert (Memory Efficient)
                def findings_generator():
                    for f in findings:
                        rem_action = None
                        rem_value = None
                        if f.remediation_proposal:
                            rem_action = f.remediation_proposal.action_type
                            rem_value = str(f.remediation_proposal.new_value)

                        yield (
                            timestamp,
                            f.entity_uid,
                            f.entity_type,
                            f.field_name,
                            str(f.value),
                            f.reason,
                            f.patient_id,
                            rem_action,
                            rem_value,
                            "{}"
                        )

                cur.executemany("""
                    INSERT INTO phi_findings
                    (timestamp, entity_uid, entity_type, field_name, value, reason, patient_id, remediation_action, remediation_value, details_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, findings_generator())

                conn.commit()
                self.logger.info("Findings saved.")

        except sqlite3.Error as e:
            self.logger.error(f"Failed to save findings: {e}")

    def load_findings(self) -> List[PhiFinding]:
        """
        Loads all findings from the database.

        Returns:
            List[PhiFinding]: All persisted PHI findings.
        """
        findings = []
        if self.db_path != ":memory:" and not os.path.exists(self.db_path):
            return findings

        try:
            with self._get_connection() as conn:
                # conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                # Check if table exists (backward compatibility for old DBs if init didnt run on them)
                # But _init_db runs on __init__, so schema should be there.

                rows = cur.execute("SELECT * FROM phi_findings ORDER BY id").fetchall()

                for r in rows:
                    if r['remediation_action']:
                        prop = PhiRemediation(
                            r['remediation_action'],
                            r['field_name'],
                            r['remediation_value'],
                            None)
                    else:
                        prop = None

                    f = PhiFinding(
                        entity_uid=r['entity_uid'],
                        entity_type=r['entity_type'],
                        field_name=r['field_name'],
                        value=r['value'],
                        reason=r['reason'],
                        patient_id=r['patient_id'],
                        remediation_proposal=prop
                    )
                    findings.append(f)

        except sqlite3.Error as e:
            self.logger.error(f"Failed to load findings: {e}")

        return findings

    def compact_sidecar(self) -> Dict[str, Tuple[int, int]]:
        """
        Reclaims disk space by rewriting the sidecar file.

        Removes unreferenced (orphaned) pixel data left by deletions or
        updates, then rewrites the database offsets to match.

        The file is rewritten first and the database updated second. Those
        two hold the same fact in two places, and the window between them is
        the whole risk of this operation: a database describing a layout the
        file does not hold produces silent garbage rather than an error,
        because every read lands at a plausible-looking wrong offset. The
        ordering here, and the rollback in each direction, exist to keep the
        two on the same generation whatever fails.

        Returns:
            Dict[str, Tuple[int, int]]: A map of SOP Instance UIDs to their new (offset, length).
        """
        self.logger.info("Starting Sidecar Compaction...")
        start_time = time.time()

        live_rows, orphan_ids = self._read_blob_index()
        if not live_rows:
            self.logger.info("No live pixels found in sidecar. Compaction skipped.")
            return {}

        temp_path = self.sidecar_path + ".compact.tmp"
        backup_path = self.sidecar_path + ".compact.bak"
        original_size = os.path.getsize(self.sidecar_path)

        try:
            updates, uid_map, written_bytes = self._rewrite_live_frames(
                live_rows, temp_path)

            # Swap before the database write, never after. The database step
            # is irreversible -- it deletes orphan rows and rewrites every
            # offset -- so if it committed first and this swap then failed,
            # the database would describe a compacted layout while the file
            # on disk was still the old one. Swapping first, and rolling the
            # swap back if the database write fails, keeps the two together
            # in both directions.
            self._swap_in_compacted_sidecar(temp_path, backup_path)
            try:
                self._apply_new_offsets(orphan_ids, updates)
            # BaseException deliberately: the offsets in the database and the
            # bytes in the sidecar must not be left disagreeing, whatever
            # interrupted us -- including a KeyboardInterrupt.
            except BaseException:
                self._restore_original_sidecar(temp_path, backup_path)
                raise

            os.remove(backup_path)

            self.sidecar = SidecarManager(self.sidecar_path)
            self._log_compaction_result(start_time, original_size, written_bytes)
            return uid_map

        except Exception as exc:
            self.logger.error(f"Compaction Failed: {exc}")
            self._discard_compaction_artefacts(temp_path, backup_path)
            raise

    def _read_blob_index(self):
        """Reads which sidecar blobs are still live, and which are orphans.

        Returns:
            Tuple of (live rows ordered by offset, orphan `instance_blobs.id`
            values as single-element tuples ready for `executemany`).
        """
        try:
            with self._get_connection() as conn:
                cur = conn.cursor()

                # The ingest path writes pixel frames through SidecarManager
                # directly and never calls persist_pixel_data(), so
                # instances.pixel_offset can hold references instance_blobs
                # has never seen. Back-fill BEFORE the SELECT or compaction
                # silently discards every such blob as dead space.
                self._backfill_legacy_blobs(conn)

                # A blob is live only while its owning instance row exists.
                # This preserves the pre-blob-table orphan semantics: deleting
                # an instance must let compaction reclaim its bytes.
                live_rows = cur.execute("""
                    SELECT b.id AS id,
                           b.instance_uid AS sop_instance_uid,
                           b.kind AS kind,
                           b.offset AS pixel_offset,
                           b.length AS pixel_length
                    FROM instance_blobs b
                    WHERE b.offset IS NOT NULL
                      AND EXISTS (
                          SELECT 1 FROM instances i
                          WHERE i.sop_instance_uid = b.instance_uid
                      )
                    ORDER BY b.offset ASC
                """).fetchall()

                # Bytes for these rows are about to be discarded, so their
                # offsets would dangle into the rewritten file. Collect the
                # exact ids now and drop them only if the rewrite succeeds.
                orphan_ids = [
                    (row['id'],) for row in cur.execute("""
                        SELECT b.id AS id
                        FROM instance_blobs b
                        WHERE NOT EXISTS (
                            SELECT 1 FROM instances i
                            WHERE i.sop_instance_uid = b.instance_uid
                        )
                    """).fetchall()
                ]
            return live_rows, orphan_ids
        except sqlite3.Error as exc:
            self.logger.error(f"Compaction Failed (Query): {exc}")
            raise

    def _rewrite_live_frames(self, rows, temp_path):
        """Copies every live frame into a new file, back to back.

        Rows arrive ordered by their current offset, so the read head only
        moves forward through a file that may be many gigabytes.

        Returns:
            Tuple of (updates, uid_map, bytes written), where `updates` is
            (new_offset, new_length, instance_blobs.id) per row.
        """
        updates = []
        uid_map = {}
        current_out_pos = 0

        with open(self.sidecar_path, "rb") as f_in, open(temp_path, "wb") as f_out:
            for row in rows:
                if row['pixel_length'] <= 0:
                    continue

                f_in.seek(row['pixel_offset'])
                data = f_in.read(row['pixel_length'])

                if len(data) != row['pixel_length']:
                    self.logger.warning(
                        "Compaction Warning: Unexpected EOF for instance ID %s",
                        row['id'])

                f_out.write(data)
                length = len(data)

                # Offset and length always travel together, so the pair can
                # never be assembled from two different generations.
                updates.append((current_out_pos, length, row['id']))

                # uid_map is keyed by UID alone and is consumed by
                # DicomSession.compact() to patch _pixel_loader. Adding a
                # non-pixel kind here would point the pixel loader at the
                # wrong blob, so only pixel rows are published.
                if row['kind'] == 'pixels':
                    uid_map[row['sop_instance_uid']] = (current_out_pos, length)

                current_out_pos += length

        return updates, uid_map, current_out_pos

    def _swap_in_compacted_sidecar(self, temp_path, backup_path):
        """Moves the rewritten file into place, keeping the original aside.

        The paths share a directory by construction, so `os.replace` is
        atomic and these renames cost nothing.
        """
        if os.path.exists(backup_path):
            os.remove(backup_path)
        os.replace(self.sidecar_path, backup_path)
        try:
            os.replace(temp_path, self.sidecar_path)
        # BaseException deliberately: a KeyboardInterrupt landing between
        # these two renames would otherwise leave the sidecar missing
        # entirely. Restore first, then let it propagate.
        except BaseException:
            os.replace(backup_path, self.sidecar_path)
            raise

    def _restore_original_sidecar(self, temp_path, backup_path):
        """Undoes the swap, so the database never describes a file it lost."""
        os.replace(self.sidecar_path, temp_path)
        os.replace(backup_path, self.sidecar_path)

    def _apply_new_offsets(self, orphan_ids, updates):
        """Points the database at the rewritten file, in one transaction.

        `updates` holds (new_offset, new_length, **instance_blobs.id**) --
        not `instances.id`. Updating `instances` by that id would corrupt
        unrelated rows, so the legacy columns are patched by UID through a
        lookup on the blob table instead. Both columns are written together:
        patching only the offset would leave `instances` with an offset from
        the new generation and a length from the old one, which reads as
        truncated data rather than as an error.
        """
        with self._get_connection() as conn:
            if orphan_ids:
                conn.executemany(
                    "DELETE FROM instance_blobs WHERE id = ?", orphan_ids)
            conn.executemany("""
                UPDATE instance_blobs SET offset = ?, length = ?
                WHERE id = ?
            """, updates)
            conn.executemany("""
                UPDATE instances SET pixel_offset = ?, pixel_length = ?
                WHERE sop_instance_uid = (
                    SELECT instance_uid FROM instance_blobs
                    WHERE id = ? AND kind = 'pixels'
                )
            """, updates)

    def _discard_compaction_artefacts(self, temp_path, backup_path):
        """Removes the working files a failed compaction left behind."""
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError as exc:
                self.logger.warning(
                    "Could not remove temporary sidecar %s: %s", temp_path, exc)

        # Only ever discard the backup once the real sidecar is back in
        # place -- otherwise it is the last copy of the data.
        if os.path.exists(backup_path) and os.path.exists(self.sidecar_path):
            try:
                os.remove(backup_path)
            except OSError as exc:
                self.logger.warning(
                    "Could not remove stale sidecar backup %s: %s",
                    backup_path, exc)

    def _log_compaction_result(self, start_time, original_size, written_bytes):
        """Reports how much space the rewrite reclaimed."""
        duration = time.time() - start_time
        saved = original_size - written_bytes

        self.logger.info(
            "Compaction Complete in %.2fs. Size: %d -> %d bytes. "
            "Reclaimed: %d bytes.",
            duration, original_size, written_bytes, saved)

        megabyte = 1024 * 1024
        print(f"Compaction Complete. "
              f"Size: {original_size / megabyte:.2f}MB -> "
              f"{written_bytes / megabyte:.2f}MB. "
              f"Reclaimed: {saved / megabyte:.2f}MB.")


class IsocenterJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, bytes):
            return {"__type__": "bytes", "data": base64.b64encode(obj).decode('ascii')}

        if isinstance(obj, MultiValue):
            return list(obj)

        return super().default(obj)


def isocenter_json_object_hook(d):
    if "__type__" in d and d["__type__"] == "bytes":
        return base64.b64decode(d["data"])
    return d

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
import hashlib
import base64
import traceback
from typing import List, Optional, Dict, Any, Tuple, NamedTuple
from dataclasses import dataclass
from datetime import datetime
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

_UPSERT_INSTANCE_SQL = """
    INSERT INTO instances (series_id_fk, sop_instance_uid, sop_class_uid, instance_number, file_path,
                           pixel_offset, pixel_length, pixel_hash, compress_alg, attributes_json,
                           phi_status)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(sop_instance_uid) DO UPDATE SET
        series_id_fk=excluded.series_id_fk,
        sop_class_uid=excluded.sop_class_uid,
        instance_number=excluded.instance_number,
        file_path=excluded.file_path,
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



class SqliteStore:
    """
    Handles persistence of the Object Graph to a SQLite database.

    This class manages:
    - CRUD operations for the Patient->Study->Series->Instance hierarchy.
    - Sidecar retrieval and compaction logic.
    - An asynchronous Audit Log for tracking modifications and errors.
    """

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
        loss_scope TEXT
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
        self._audit_thread = threading.Thread(
            target=self._audit_worker, daemon=True, name="AuditWorker")
        self._audit_thread.start()

    def __getstate__(self):
        """Exclude threading primitives from pickling."""
        state = self.__dict__.copy()
        keys_to_remove = [
            '_memory_lock',
            '_memory_conn',
            'audit_queue',
            '_stop_event',
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
        self._audit_thread = threading.Thread(
            target=self._audit_worker, daemon=True, name="AuditWorker")
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
            conn = sqlite3.connect(self.db_path, timeout=900.0)
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

        Args:
            instance (Instance): The hydrated instance; its attributes and
                sequences must already be restored, because the loader reads
                its geometry out of the Waveform Sequence.
            wref: A row from `instance_blobs` (kind 'waveform'), or None.
        """
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

    def _audit_worker(self):
        """Background thread to batch write audit logs."""
        batch = []
        while not self._stop_event.is_set():
            try:
                # Collect items with timeout
                try:
                    item = self.audit_queue.get(timeout=1.0)
                    batch.append(item)

                    # Drain queue up to limit
                    while len(batch) < 100:
                        try:
                            item = self.audit_queue.get_nowait()
                            batch.append(item)
                        except queue.Empty:
                            break

                except queue.Empty:
                    pass

                if batch:
                    self.log_audit_batch(batch)
                    batch = []

            except Exception as e:
                # Don't crash thread
                self.logger.error(f"Audit Worker Error: {e}")

        # Flush remaining
        while not self.audit_queue.empty():
            try:
                batch.append(self.audit_queue.get_nowait())
            except queue.Empty:
                break
        if batch:
            self.log_audit_batch(batch)

    def stop(self):
        """Stops the audit worker and flushes queue."""
        self._stop_event.set()
        if self._audit_thread.is_alive():
            self._audit_thread.join(timeout=2.0)
        self.flush_audit_queue()

    def flush_audit_queue(self):
        """Manually processes all pending items in the audit queue."""
        batch = []
        while not self.audit_queue.empty():
            try:
                batch.append(self.audit_queue.get_nowait())
            except queue.Empty:
                break

        if batch:
            self.log_audit_batch(batch)

    def log_audit(self, action_type: str, entity_uid: str, details: str,
                  loss_scope: Optional[str] = None):
        """Records an action in the audit log (Async).

        Args:
            action_type (str): e.g. 'EXPORT', 'ERROR', 'DATA_LOSS'.
            entity_uid (str): The instance (or path) the action concerns.
            details (str): Prose for the human reading the report.
            loss_scope (str, optional): For `DATA_LOSS` only:
                `io_handlers.LOSS_SCOPE_PRIVATE` or
                `LOSS_SCOPE_STANDARD`. This is what `generate_report`
                grades on, and it is passed in rather than derived from
                `details` because only the caller still holds the tag
                (#146).
        """
        # Push to queue instead of writing directly
        self.audit_queue.put((action_type, entity_uid, details, loss_scope))

    def get_audit_summary(self) -> Dict[str, int]:
        """
        Returns an aggregated summary of actions from the audit log.
        Stops and restarts the background audit worker to ensure consistency.
        Returns:
            Dict[str, int]: e.g., {'ANONYMIZE': 500, 'EXPORT': 500}
        """
        # Stop worker to ensure all in-flight batches are written
        # This joins the thread and flushes the queue.
        self.stop()

        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                try:
                    cursor.execute(
                        "SELECT action_type, COUNT(*) FROM audit_log GROUP BY action_type")
                    rows = cursor.fetchall()
                    return {row[0]: row[1] for row in rows}
                except sqlite3.OperationalError:
                    return {}
        finally:
            # Restart the worker
            self._stop_event.clear()
            self._audit_thread = threading.Thread(
                target=self._audit_worker, daemon=True, name="AuditWorker")
            self._audit_thread.start()

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
        `PRIVATE` now takes `validation_status` to `REVIEW_REQUIRED`;
        one scoped `STANDARD` leaves it at `PASS` (CHANGELOG.md, #146).
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

    def log_audit_batch(self, entries: List[tuple]):
        """
        Batch inserts audit logs.

        entries: List of (action_type, entity_uid, details, loss_scope).
        `loss_scope` is None for everything that is not a `DATA_LOSS`
        row; a caller with no loss to describe still writes the slot,
        because one record with two accepted shapes is a fork the
        reader has to hold in their head.
        """
        if not entries:
            return

        timestamp = datetime.now().isoformat()
        # (timestamp, action, uid, details, loss_scope)
        data = [(timestamp, e[0], e[1], e[2], e[3]) for e in entries]

        try:
            with self._get_connection() as conn:
                conn.executemany(
                    "INSERT INTO audit_log (timestamp, action_type, entity_uid, details, loss_scope) "
                    "VALUES (?, ?, ?, ?, ?)", data)
                conn.commit()
        except sqlite3.Error as e:
            self.logger.error(f"Failed to batch log audit: {e}")

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
                    st = Study(r['study_instance_uid'], r['study_date'])
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
                    st = Study(st_r['study_instance_uid'], st_r['study_date'])
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
                            stored_statuses.append((inst, r['phi_status']))

                        st.series.append(se)
                    p.studies.append(st)

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
        Uses UPSERT semantics (Delete-Insert logic currently).

        Args:
            instance_uid (str): The SOP Instance UID.
            attributes (Dict[Tuple[str, str], Any]): Mapping of (Group, Element) hex strings to values.
            conn (sqlite3.Connection, optional): An existing database connection to use for the transaction.
        """
        if not attributes:
            return

        data_rows = []
        for (grp, elem), val in attributes.items():
            vr = "UN"  # Todo: Pass VR from caller
            # Check for VM > 1
            if isinstance(val, list):
                for idx, atom in enumerate(val):
                    data_rows.append((instance_uid, grp, elem, idx, vr, str(atom)))
            else:
                data_rows.append((instance_uid, grp, elem, 0, vr, str(val)))

        if not data_rows:
            return

        try:

            # If conn is passed, use it (and don't close it/commit it here, leave to caller).
            # If not, create new context (which commits/closes).
            ctx = self._get_connection() if conn is None else nullcontext(conn)

            with ctx as db:
                # 1. OPTIMIZATION: Delete existing for these keys first?
                # Or UPSERT.
                # "test_vertical_update_serialization" requires correctness.
                # UPSERT based on unique index (uid, grp, elem, atom) works.
                # But if list shrinks (VM 3 -> VM 1), UPSERT leaves atoms 2,3.
                # So we MUST DELETE by (uid, grp, elem) before inserting new set for that tag.

                # We can do this in transaction.
                keys_to_clear = list(attributes.keys())
                # Batch delete?
                # "DELETE FROM instance_attributes WHERE instance_uid=? AND group_id=? AND element_id=?\"
                del_params = [(instance_uid, k[0], k[1]) for k in keys_to_clear]
                db.executemany(
                    "DELETE FROM instance_attributes WHERE instance_uid=? AND group_id=? AND element_id=?",
                    del_params)

                db.executemany("""
                    INSERT INTO instance_attributes (instance_uid, group_id, element_id, atom_index, value_rep, value_text)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, data_rows)

        except sqlite3.Error as e:
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
        results = {}
        try:
            with self._get_connection() as conn:
                rows = conn.execute("""
                    SELECT group_id, element_id, atom_index, value_text
                    FROM instance_attributes
                    WHERE instance_uid=?
                    ORDER BY group_id, element_id, atom_index
                """, (instance_uid,)).fetchall()

                if not rows:
                    return {}

                # Reassemble
                curr_key = None
                collect = []

                for r in rows:
                    key = (r['group_id'], r['element_id'])
                    val = r['value_text']  # Type conversion? Strings for now.

                    if key != curr_key:
                        # Flush previous
                        if curr_key:
                            results[curr_key] = collect if len(collect) > 1 else collect[0]
                        curr_key = key
                        collect = [val]
                    else:
                        collect.append(val)

                # Flush last
                if curr_key:
                    results[curr_key] = collect if len(collect) > 1 else collect[0]

            return results
        except sqlite3.Error as e:
            self.logger.error(f"Failed to load vertical attributes for {instance_uid}: {e}")
            return {}

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
        if instance.pixel_array is None:
            return

        try:
            # 1. Write to Sidecar
            # Pass array directly to avoid .tobytes() Memory spike (Zero-Copy 500MB save)
            b_data = instance.pixel_array

            # Hash Update (CRITICAL for Integrity Checks)
            # Calculate Hash BEFORE writing/compression to ensure we capture the state
            # exactly as it goes into the pipe.
            import hashlib
            # Ensure we are hashing the contiguous bytes
            if hasattr(b_data, 'tobytes'):
                p_hash = hashlib.sha256(b_data.tobytes()).hexdigest()
            else:
                p_hash = hashlib.sha256(b_data).hexdigest()

            instance._pixel_hash = p_hash

            # Determine suitable compression? Defaulting to zlib for swap.
            # Ideally we respect original or config, but for swap zlib is safe/fast enough.
            c_alg = 'zlib'

            offset, length = self.sidecar.write_frame(b_data, c_alg)

            # 2. Update Instance Loader
            # This allows instance.unload_pixel_data() to work safely
            # Note: instance attributes ARE populated here (it's a live object), so
            # passing instance=instance works.
            instance._pixel_loader = self._create_pixel_loader(
                offset, length, c_alg, instance, pixel_hash=p_hash)

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

        try:
            with self._get_connection() as conn:
                cur = conn.cursor()
                pending_deletions = []
                for patient in patients:
                    saved_instances.extend(
                        self._save_patient(conn, cur, patient, tally,
                                           pending_deletions))

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
                      pending_deletions) -> List[Tuple[Instance, int]]:
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
                        conn, cur, series, series_pk, tally))

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
                INSERT INTO studies (patient_id_fk, study_instance_uid, study_date, phi_status)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(study_instance_uid) DO UPDATE SET
                    study_date=excluded.study_date,
                    patient_id_fk=excluded.patient_id_fk,
                    phi_status=excluded.phi_status
            """, (patient_pk, study.study_instance_uid,
                  _as_stored_date(study.study_date),
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
                              tally) -> List[Tuple[Instance, int]]:
        """Upserts the instances of one series that have unsaved changes.

        Returns the (instance, revision) pairs written, for the caller to
        mark persisted once the transaction has actually committed. The
        revision is captured *before* the write, so a concurrent edit
        arriving mid-save is not mistaken for the state that was stored.
        """
        unsaved = [(inst, inst._revision)
                   for inst in series.instances if inst.has_unsaved_changes]
        if not unsaved:
            return []

        rows, blob_rows, vertical_rows = self._build_instance_writes(
            [inst for inst, _revision in unsaved], series_pk, tally)

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

    def _build_instance_writes(self, instances, series_pk, tally):
        """Turns instances into the rows three tables need.

        Pixel data is written to the sidecar here, because the offset it
        lands at is part of the row. Nothing else touches the database:
        the caller decides when, and in what order, these go in.

        Returns:
            Tuple of (instance rows, instance_blobs rows, (uid, private
            attributes) pairs for the vertical table).
        """
        rows, blob_rows, vertical_rows = [], [], []

        for inst in instances:
            core, private = _split_core_and_private(self._serialize_item(inst))
            if private:
                vertical_rows.append((inst.sop_instance_uid, private))

            frame = self._persist_pixels(inst, tally)
            rows.append((
                series_pk, inst.sop_instance_uid, inst.sop_class_uid,
                inst.instance_number, inst.file_path,
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

    def _persist_pixels(self, inst, tally) -> '_StoredFrame':
        """Writes this instance's pixels to the sidecar if they are new.

        Three cases: pixels resident in memory (hash them, and write only
        if the bytes actually changed), pixels already swapped out to the
        sidecar (keep the reference the loader holds), or no pixels at all.
        """
        if inst.pixel_array is None:
            loader = inst._pixel_loader
            if isinstance(loader, SidecarPixelLoader):
                return _StoredFrame(loader.offset, loader.length, loader.alg,
                                    getattr(inst, '_pixel_hash', None))
            return _StoredFrame(None, None, None, None)

        raw = inst.pixel_array.tobytes()
        digest = hashlib.sha256(raw).hexdigest()
        loader = inst._pixel_loader

        # Deduplication: identical bytes already in the sidecar. Appending
        # them again would grow the file by a full frame per save.
        if (getattr(inst, '_pixel_hash', None) == digest
                and isinstance(loader, SidecarPixelLoader)):
            inst._pixel_hash = digest
            return _StoredFrame(loader.offset, loader.length, loader.alg, digest)

        offset, length = self.sidecar.write_frame(raw, _PIXEL_COMPRESSION)
        tally.pixel_bytes += length
        tally.pixel_frames += 1

        # Re-point the loader so the array can be unloaded safely later.
        inst._pixel_loader = self._create_pixel_loader(
            offset, length, _PIXEL_COMPRESSION, inst)
        inst._pixel_hash = digest
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
                                instance_uids: List[str] = None):
        """
        Yields a flat dictionary for every instance in the DB.

        Useful for streaming exports or analysis without loading the entire graph into RAM.

        Args:
            patient_ids (List[str], optional): Filter by list of Patient IDs.
            instance_uids (List[str], optional): Filter by list of SOP Instance UIDs.

        Yields:
            dict: Flattend dictionary representing row data (patient, study, series, instance paths).
        """
        # We use a managed connection that stays open during iteration
        with self._get_connection() as conn:
            # conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            query = """
                SELECT
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

            conditions = []
            params = []

            if patient_ids:
                placeholders = ",".join("?" for _ in patient_ids)
                conditions.append(f"p.patient_id IN ({placeholders})")
                params.extend(patient_ids)

            if instance_uids:
                placeholders = ",".join("?" for _ in instance_uids)
                conditions.append(f"i.sop_instance_uid IN ({placeholders})")
                params.extend(instance_uids)

            if conditions:
                query += " WHERE " + " AND ".join(conditions)

            # Execute generator
            cursor = cur.execute(query, params)

            # We can map columns to names
            cols = [desc[0] for desc in cursor.description]

            for row in cursor:
                yield dict(zip(cols, row))

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

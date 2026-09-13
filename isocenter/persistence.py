"""
Persistence layer for Isocenter.

This module provides the SqliteStore class which manages the storage and retrieval
of DICOM entities (Patients, Studies, Series, Instances) using a SQLite database.
It also handles sidecar storage for pixel data to keep the database lightweight.
"""

import fcntl
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
import secrets
import traceback
from collections import Counter
from typing import List, Optional, Dict, Any, Set, Tuple, NamedTuple
from dataclasses import dataclass
from datetime import date, datetime
from contextlib import nullcontext

from pydicom.multival import MultiValue

from .entities import (Patient, Study, Series, Instance, Equipment,
                       PhiStatus, normalize_study_date, resolve_item_path)
from . import entities
from .blob_kind import parse_blob_kind, serialize_blob_kind
from .sidecar import SidecarManager
from .logger import describe_exception, get_logger
from .privacy import (PhiFinding, PhiRemediation, _is_keyed_pseudonym_shape,
                      _is_replacement_id, _is_unkeyed_pseudonym_shape,
                      _pseudonym_verifies, _unkeyed_replacement_id_for)
from .io_handlers import (NestedPixelRef, SidecarPixelLoader,
                          nested_item_geometry)



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
                           phi_status, shift_provenance)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(sop_instance_uid) DO UPDATE SET
        series_id_fk=excluded.series_id_fk,
        sop_class_uid=excluded.sop_class_uid,
        instance_number=excluded.instance_number,
        file_path=excluded.file_path,
        source_path=COALESCE(excluded.source_path, instances.source_path),
        attributes_json=excluded.attributes_json,
        phi_status=excluded.phi_status,
        -- Plain assignment, deliberately NOT COALESCE-guarded like the
        -- four below (#510). A legacy instance has to be able to write
        -- its NULL: guarded, the first save after the upgrade would
        -- stamp 'recorded' over it and the instance would lose the
        -- protection its unrecorded dates depend on -- one re-save and
        -- every one of them gets shifted a second time.
        shift_provenance=excluded.shift_provenance,
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


class _HeldUids(NamedTuple):
    """Every study, series and instance UID a list of patients holds."""
    studies: Set[str]
    series: Set[str]
    instances: Set[str]


def _held_uids(patients) -> _HeldUids:
    """What a save must not delete: each UID any object in `patients` holds.

    One walk over the graph, read at the moment the scoped deletes run,
    so a UID renamed in place since the prepass is read as it is now
    (#548; `SqliteStore.save_all`).
    """
    held = _HeldUids(set(), set(), set())
    for patient in patients:
        for study in patient.studies:
            held.studies.add(study.study_instance_uid)
            for series in study.series:
                held.series.add(series.series_instance_uid)
                for instance in series.instances:
                    held.instances.add(instance.sop_instance_uid)
    return held


def _warn_on_shared_patient_ids(logger, patients) -> None:
    """One WARNING when two `Patient` objects in a save carry one ID (#548).

    The store holds one `patients` row per ID, so the row's name and
    status come from whichever object the walk reaches last; every study
    is kept (`_held_uids`). A counts-only line: since 0.9.7 the log file
    names no Patient ID, because a log shipped beside an export would
    otherwise pair identities with what replaced them.
    """
    per_id = Counter(p.patient_id for p in patients)
    sharing = sum(n for n in per_id.values() if n > 1)
    if sharing:
        logger.warning(
            "%d Patient objects share a Patient ID with another; the store "
            "holds one row for each ID and keeps every study; reload to see "
            "them as one patient", sharing)


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

#: How long a sidecar writer, a redact()/ingest() pass, or `compact()`
#: waits for the sidecar gate or the pass-lock before raising (#368).
#: Its place in the timeout family, with what each bound protects:
#:
#:     _SQLITE_BUSY_TIMEOUT_S = 120  <  this = 180
#:         <  _WORKER_FAULTHANDLER_TIMEOUT_S = 240  <  faulthandler_timeout = 300
#:
#: Above 120 s plus a frame write: the gate is the one lock deliberately
#: held across a sqlite write, so a waiter behind a holder that is
#: itself waiting out the busy timeout must not give up first -- it
#: would raise a gate error that misnames the fault (the database is
#: what is stuck) and, on the save path, leave the instances dirty when
#: the holder was seconds from succeeding. Below 240 and 300 s: a stuck
#: gate must error inside both faulthandler windows so the dump shows a
#: thread *waiting at the gate* with a stack, and the error, not the job
#: cap, ends the test (#280, #250). `tests/test_packaging_contract.py`
#: pins the inequality and, by `inspect.getsource`, that
#: `_hold_sidecar_gate` still READS this name. No environment variable
#: on purpose (one spelling per behaviour); tests monkeypatch the
#: constant. The value tolerates a compaction of ~800 MB live on
#: 100 MB/s storage behind a stuck sqlite writer; a healthy compaction
#: holds the gate for 0.217 s/GB on local SSD (2026-09-08 spec §2.3).
_SIDECAR_GATE_TIMEOUT_S = 180.0

#: The poll interval for the two bounded flock loops. `flock` has no
#: timeout and `signal.alarm` does not reach non-main threads, so the
#: bound is a `LOCK_NB` attempt every 10 ms. The uncontended path takes
#: the lock on the first attempt (11.6 us measured); a contended one
#: pays at most one interval on top of the hold it waited for.
_LOCK_POLL_INTERVAL_S = 0.01


@contextlib.contextmanager
def _flock_within(path, flags, deadline, describe):
    """Hold `flags` on `path` for the block, polling `LOCK_NB` until `deadline`.

    One fd per acquisition, opened here and closed on every exit
    including exception: a leaked fd holds the flock for the life of
    the process, and `flock` is per open file description, so a second
    fd on the same path from the same thread would deadlock against the
    first (the self-deadlock the 2026-09-07 spec measured). `describe`
    is called only on expiry, to build the error.
    """
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        while True:
            try:
                fcntl.flock(fd, flags | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(describe()) from None
                time.sleep(_LOCK_POLL_INTERVAL_S)
        try:
            yield fd
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


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
    used to diverge, because `export_folder_names` builds it with
    `str(study.study_date or "NoDate")` rather than
    `format_study_date`: a hand-built graph carrying `"20240101"` filed
    under `Study_20240101_` before a round trip and `Study_2024-01-01_`
    after one, so the same study occupied two directories and a
    re-export into an existing tree wrote a second copy instead of
    overwriting the first. Closed at the other end of the same rule --
    `Study.__setattr__` normalises on assignment -- rather than in
    `export_folder_names`, which would have renamed every *ingested*
    study's directory (#189).

    The body is `entities.normalize_study_date`, and it must stay that
    way: two copies of "text that names a day becomes a `date`" is
    exactly how the two ends came to disagree.
    """
    return normalize_study_date(value)


#: How to read a `value_text` back for the VRs whose values are *not*
#: text on the wire. The vertical table's column is TEXT, so every value
#: is stored stringified; for these VRs pydicom refuses the string at
#: write time -- measured on 3.0.2, `US`/`UL`/`FL` raise
#: `struct.error: required argument is not an integer` from
#: `filewriter.write_numbers`, which fails the whole export rather than
#: the element. Restoring the type is therefore part of restoring the
#: VR, not a separate nicety (#154).
#:
#: The text VRs are deliberately absent. `SH`, `LO`, `UT`, `DS` and `IS`
#: all write correctly from a `str` -- `DS` and `IS` *are* text on the
#: wire -- so converting them would invent a Python type the source
#: never had, which is the "reconstructing a type by inspecting the
#: value" this table exists to avoid.
_VERTICAL_VR_PARSERS = {
    'US': int, 'SS': int, 'UL': int, 'SL': int, 'UV': int, 'SV': int,
    'AT': int, 'FL': float, 'FD': float,
}


def _vertical_atom_text(vr: str, atom: Any) -> Optional[str]:
    """The one rendering of a private value into `value_text`.

    `str(atom)` for everything except `AT`, whose `str()` is the display
    spelling `'(0010,0010)'` -- which `Tag()` refuses to read back
    ("unknown DICOM element keyword or an invalid int"). Stored as the
    decimal integer it is, so `_VERTICAL_VR_PARSERS['AT']` is its exact
    inverse. This pair has to stay a pair.

    `None` is the one value that is not stringified, and it is a SQL
    NULL instead (#339). `str(None)` is the four-character text `None`,
    which is a conformant `LO` value, so a zero-length private element
    -- what pydicom hands back for `DS`, `US`, `AT`, `UN` and their
    siblings when the source wrote no value -- reloaded as a word the
    source never said and was exported as though it had. Absent beats
    fabricated, which is the ruling #60 made for a missing Study Date.

    The guard has to come BEFORE the `AT` arm. Without it `int(None)`
    raises `TypeError`, the arm's own `except` catches it, and it
    returns `str(atom)` -- the same fabricated `'None'`, arriving
    through a handler documented for "a value that is no longer a tag".
    """
    if atom is None:
        return None
    if vr == 'AT':
        try:
            return str(int(atom))
        except (TypeError, ValueError):
            # A value that is no longer a tag. Stored as it reads and
            # reloaded as text; `_value_fits_vr` refuses it at export
            # and the fallback runs, which is the pre-#154 behaviour.
            return str(atom)
    return str(atom)


def _vertical_atom_value(vr: str, text: Optional[str]) -> Any:
    """The inverse of `_vertical_atom_text`, keyed on the stored VR.

    A NULL `value_text` is the atom that was `None` (#339), and nothing
    else can produce one: every other arm above goes through `str()`,
    which never returns `None`, so no row written by any released
    version can be NULL here.
    """
    if text is None:
        # Behaviour-equivalent to falling through, and written anyway.
        # A numeric VR would reach `int(None)` -> `TypeError` -> the
        # `except` arm below, and a text VR would return the `None`
        # because `parser is None` -- both answer `None` already. But
        # that `except` arm's comment says "keep the text", and a SQL
        # NULL is not text: leaving the normal path to run through an
        # exception handler documented for something else would make
        # the arm's own comment false.
        return None
    parser = _VERTICAL_VR_PARSERS.get(vr)
    if parser is None:
        return text
    try:
        return parser(text)
    except (TypeError, ValueError):
        # Unparseable text under a numeric VR: keep the text. The VR
        # then fails `_value_fits_vr` at export and the value takes the
        # fallback, which is exactly what it did before #154 -- never
        # worse than no recorded VR at all.
        return text


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
            store.logger.error(f"Audit Worker Error: {describe_exception(e)}")
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
    #:
    #: Private, and the underscore is the smaller half of why (#202).
    #: This is the *default* and not a knob: `get_flattened_instances`
    #: takes it as a default argument, which Python evaluates once when
    #: the `def` runs, so the number is baked into `__defaults__` at
    #: import and rebinding this attribute -- on the class or on a
    #: subclass -- changes nothing. Measured: after setting it to `2`, a
    #: default-argument walk over six rows still took one connection.
    #: `page_size=` is the one spelling for the behaviour.
    _FLATTENED_PAGE_SIZE = 500

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS patients (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT NOT NULL,
        patient_name TEXT,
        phi_status TEXT,
        -- 'keyed-hmac-v1' or 'unkeyed-sha256'; fixed once per patient,
        -- NULL only in a row a release before 0.9.7 wrote
        jitter_scheme TEXT,
        UNIQUE(patient_id)
    );

    -- The project secret that keys every keyed pseudonym and date offset
    -- in this store. One row, created on first need. Never exported.
    CREATE TABLE IF NOT EXISTS project_secret (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        secret_hex TEXT NOT NULL,
        origin TEXT NOT NULL,     -- 'generated' | 'loaded'
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS studies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id_fk INTEGER,
        study_instance_uid TEXT NOT NULL,
        study_date TEXT,
        date_shifted INTEGER,
        shifted_study_date TEXT, -- what a shift produced; NULL pre-0.9.6
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
        shift_provenance TEXT, -- 'recorded' since 0.9.6; NULL means pre-0.9.6
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
        value_count INTEGER,
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
            # This store created the temp file, so `stop()` unlinks it
            # (and the two lock files beside it). Ownership is a flag
            # rather than a re-derivation from `db_path` because a
            # pickled clone -- a spawned worker's copy -- shares the
            # *same* sidecar path and must not unlink it: `__getstate__`
            # drops the flag and `__setstate__` sets it False (#376).
            self._owns_temp_sidecar = True
            # Shared memory connection for :memory: database to persist across transactions
            self._memory_conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._memory_conn.row_factory = sqlite3.Row
            self._memory_lock = threading.Lock()
        else:
            self.sidecar_path = os.path.splitext(db_path)[0] + "_pixels.bin"
            self._memory_conn = None
            self._memory_lock = None
            # A file-backed sidecar is data; its lock files are stable
            # paths other processes of this session may be polling.
            self._owns_temp_sidecar = False

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
        # stale hash matches the stale frame (#274). Never held across a
        # sqlite write, whose busy timeout can be waited out while
        # holding it.
        #
        # This used to document a lock order, `_pixel_swap_lock` before
        # `sidecar._lock`. There was no `sidecar._lock` to order against:
        # it was constructed and never acquired, here or anywhere (#366).
        # Writers serialise on `fcntl.flock` inside `write_frame`, which
        # is a different mechanism with different reach -- it is
        # cross-process, and `read_frame` does not take it at all.
        # Writers against `compact_sidecar` are serialised by the gate
        # below, which sits ABOVE this lock (#368).
        self._pixel_swap_lock = threading.Lock()
        # The sidecar gate's in-process half (#368). Mutual exclusion
        # between every frame writer and the compaction rewrite; the
        # cross-process half is `fcntl.flock` on `_gate_path()`, taken
        # inside `_hold_sidecar_gate` only after this lock is held, so
        # two threads of one process never hold two fds on the lock
        # file at once. Lock order, an internal invariant pinned by
        # `tests/test_sidecar_gate_order.py`:
        #
        #     _sidecar_gate -> _pixel_swap_lock          (rewire; sites 5, 6)
        #     _sidecar_gate -> sqlite                    (every site's commit)
        #     _sidecar_gate -> pass-lock, LOCK_NB only   (compact's refusal)
        #     _pixel_swap_lock -> entities.PIXEL_STATE_LOCK  (publish; leaf)
        #
        # `PIXEL_STATE_LOCK` is innermost: taken by the pixel mutators
        # holding nothing and by the publish sections under this lock, and
        # never held while taking any other lock, a flock or sqlite
        # (#434, Q6).
        #
        # The gate is the one lock deliberately held across a sqlite
        # write: the hazard it closes is a frame appended before
        # `_read_blob_index` whose row commits after `_apply_new_offsets`,
        # and only a hold spanning append AND commit closes that. It is
        # never taken inside `write_frame` (`SidecarManager` is stateless
        # and called directly by fixture generators) and never taken by
        # a thread holding `_pixel_swap_lock` (`_persist_pixels` calls
        # `write_frame` under the swap lock; `_rewire_sidecar_loaders`
        # takes the swap lock under the gate -- a cycle).
        self._sidecar_gate = threading.Lock()
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
            # The gate's thread lock. The clone recreates its own and
            # opens its own fd on the same lock path per acquisition;
            # the flock, not this lock, is what reaches across (#368).
            '_sidecar_gate',
            '_audit_thread',
            # Not a threading primitive, but dropped for the same
            # reason a clone gets fresh locks: a clone that inherited
            # `True` would unlink the parent's sidecar on its own
            # `stop()` (#376). `__setstate__` sets it False.
            '_owns_temp_sidecar']
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
        # A clone never owns the temp sidecar, whatever the parent did:
        # `tests/test_save_redact_race.py` calls `clone.stop()` on
        # pickled clones while the parent is still reading (#376).
        self._owns_temp_sidecar = False

        self.audit_queue = queue.Queue()
        self._stop_event = threading.Event()
        # See `__init__`: before the worker starts, not after (#218).
        self._audit_write_lock = threading.Lock()
        self._audit_wakeup = threading.Event()
        self._audit_drop_lock = threading.Lock()
        self._pixel_swap_lock = threading.Lock()
        self._sidecar_gate = threading.Lock()
        self._audit_thread = threading.Thread(
            target=_audit_worker_loop,
            args=(weakref.ref(self), self._stop_event, self._audit_wakeup,
                  self.audit_queue),
            daemon=True, name="AuditWorker")
        self._audit_thread.start()

    def _gate_path(self) -> str:
        """The sidecar gate's lock file: `<sidecar>.lock`.

        A stable path *beside* the sidecar, never the sidecar itself.
        `flock` binds to an inode, and `compact_sidecar` swaps the
        sidecar in with `os.replace`, which gives the path a new inode:
        a writer blocked on the old one wakes after the swap and appends
        into the unlinked file (#368). This path is never replaced.
        """
        return self.sidecar_path + ".lock"

    def _pass_lock_path(self) -> str:
        """The pass-lock's file: `<sidecar>.pass.lock` (#368)."""
        return self.sidecar_path + ".pass.lock"

    def _gate_timeout_message(self) -> str:
        """One class, one spelling: every channel carries this text."""
        return (f"Sidecar gate {self._gate_path()} not acquired within "
                f"_SIDECAR_GATE_TIMEOUT_S={_SIDECAR_GATE_TIMEOUT_S:g} s; a "
                "compaction or another writer is holding it")

    @contextlib.contextmanager
    def _hold_sidecar_gate(self):
        """Hold the sidecar gate for the block (#368).

        Mutual exclusion between every frame writer and the compaction
        rewrite. Held at the six `write_frame` sites across the append
        **and** the row commit, and by `Session.compact()` across
        `compact_sidecar()` **and** `_rewire_sidecar_loaders()`. Thread
        lock first, then an exclusive `flock` on `_gate_path()` -- the
        stable path beside the sidecar, because a flock binds to an
        inode and compaction's `os.replace` gives the sidecar a new
        one. Bounded by `_SIDECAR_GATE_TIMEOUT_S` as one budget across
        both halves; expiry raises `RuntimeError` naming the lock file
        and the constant. Where that lands: a redaction worker returns
        it as `RedactionOutcome(ok=False)` and the parent raises
        `RedactionError` with an ERROR audit row; ingest files an ERROR
        audit row per result; a background save logs `Background save
        failed` and leaves its instances dirty for the next save
        (owner's decision C1, no audit row); `save(sync=True)` and
        `compact()` raise to the caller.

        The known liveness cost, stated rather than hidden: a `close()`
        whose persistence worker is queued behind a compaction longer
        than `_SHUTDOWN_JOIN_TIMEOUT_S` (30 s -- about 3 GB live on
        local SSD, ~300 MB on 100 MB/s network storage) will have
        #314's wedged-worker machinery misfire on a healthy compaction.
        The same ordering used to corrupt the save instead. Loud and
        late beats silent and wrong; the structural fix is a two-phase
        compaction that holds the gate only for the O(delta) tail, and
        that is a filed follow-up, not this.
        """
        deadline = time.monotonic() + _SIDECAR_GATE_TIMEOUT_S
        if not self._sidecar_gate.acquire(timeout=_SIDECAR_GATE_TIMEOUT_S):
            raise RuntimeError(self._gate_timeout_message())
        try:
            with _flock_within(self._gate_path(), fcntl.LOCK_EX, deadline,
                               self._gate_timeout_message):
                yield
        finally:
            self._sidecar_gate.release()

    @contextlib.contextmanager
    def _hold_pass_lock(self):
        """Hold the pass-lock shared for a `redact()`/`ingest()` pass (#368).

        `LOCK_SH` on `_pass_lock_path()`, taken holding nothing, for
        the whole pass: from before the first worker can call
        `regenerate_uid()` until after `_apply_redaction_outcomes` has
        bound every loader. While any pass holds it, `compact()`'s
        `LOCK_EX|LOCK_NB` attempt is refused. Why a lock and not a
        predicate change: during a pass the graph carries references
        the store has not been told about yet -- a worker commits its
        blob row under a regenerated UID before any `instances` row
        names it -- and compaction's orphan predicate is *correct* to
        reclaim such a row; the fix is to keep compaction out until
        the pass has told the store, not to teach the predicate a
        second answer to "what is live". Kernel-released on any death.
        Waits, bounded by `_SIDECAR_GATE_TIMEOUT_S`, behind a running
        compaction's EX -- which now spans its leading save as well as
        the rewrite -- and then proceeds.
        """
        deadline = time.monotonic() + _SIDECAR_GATE_TIMEOUT_S
        path = self._pass_lock_path()

        def describe():
            return (f"Pass-lock {path} not acquired within "
                    f"_SIDECAR_GATE_TIMEOUT_S={_SIDECAR_GATE_TIMEOUT_S:g} s; "
                    "a compaction is saving or rewriting the sidecar")

        with _flock_within(path, fcntl.LOCK_SH, deadline, describe):
            yield

    @contextlib.contextmanager
    def _refuse_while_pass_open(self):
        """Hold the pass-lock exclusive for a compaction, or refuse (#368).

        `LOCK_EX|LOCK_NB` on `_pass_lock_path()`, taken holding nothing
        and held through the block, so a pass starting anywhere inside
        `compact()` -- during its leading save, its rewrite or its
        rewire -- waits at its `LOCK_SH`. Taken before the leading save
        rather than under the gate since the review of PR #385: a pass
        that opened and closed inside that save was admitted with its
        rows unnamed. `LOCK_NB` because the refusal is an answer, not a
        wait (a pass holds SH for its whole length, and "wait for it to
        return" is the caller's call to make). Refusal is `RuntimeError`
        before anything -- save included -- has happened.
        """
        path = self._pass_lock_path()
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError(
                    f"compact() refused: a redact() or ingest() pass is "
                    f"open on {path}; wait for it to return") from None
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

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
                    raise
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

        # `shifted_study_date` on studies (#518). `date_shifted` above
        # says a shift ran; this says what it produced, so `_scan_study`
        # can tell the shift's own output from a fresh original assigned
        # over `study_date` -- which the flag alone never could.
        #
        # Read with the flag, NULL is unambiguous and needs no second
        # provenance column:
        #
        #   False / NULL   never shifted        -> raise if there is a date
        #   True  / string shifted by >=0.9.6   -> vouched while it matches
        #   True  / NULL   shifted pre-0.9.6    -> no finding (the owner's
        #                                          ruling: nothing in an
        #                                          existing store changes)
        #   False / string a hand-edited or partially-written row -> the
        #                                          record wins, being the
        #                                          more specific claim
        #
        # The instance half needed its own `shift_provenance` column
        # precisely because `Instance.date_shifted` was never persisted,
        # so an instance row carried no witness at all. A study row
        # carries one. Naming that asymmetry is worth more than making
        # the two halves look symmetrical.
        if "shifted_study_date" not in study_columns:
            conn.execute(
                "ALTER TABLE studies ADD COLUMN shifted_study_date TEXT")

        # `shift_provenance` on instances (#510). The scan decides
        # per value whether a date has already been shifted, reading the
        # record `SHIFT_DATE` writes; an instance written before those
        # records existed carries none, and reading "no record" as "not
        # shifted" would make the first audit() after upgrading raise
        # every already-shifted date in the store and anonymize() shift
        # each one a second time.
        #
        # So NULL is **not** "not shifted" here, and this is the one
        # migration in this file where the absent column is the *unsafe*
        # reading. NULL means "this row predates per-value records", and
        # such an instance keeps the pre-0.9.6 entity-level rule for the
        # values it already holds. 'recorded' means the row was written
        # by 0.9.6 or later, so its records are the whole truth.
        #
        # No back-fill, for the same reason `value_count` has none: a
        # legacy row cannot know which of its dates were shifted, and
        # an `UPDATE ... SET shift_provenance = 'recorded'` sweep would
        # answer for exactly the rows that have no answer -- which is
        # the fabrication this column exists to prevent.
        if "shift_provenance" not in instance_columns:
            conn.execute(
                "ALTER TABLE instances ADD COLUMN shift_provenance TEXT")

        # `value_count` on instance_attributes (#328). The tier is one
        # row per value atom and recorded no arity, so a one-element
        # list reloaded as a scalar and an empty one reloaded as an
        # absent tag. The column carries the container's length on every
        # row of an element, denormalized exactly as `value_rep` is.
        #
        # Rows predating the column read NULL, and NULL keeps the old
        # rule: more than one atom is a list, one atom is a scalar.
        # There is deliberately no back-fill. An `UPDATE ... SET
        # value_count = 1` sweep would answer for exactly the rows that
        # never had an answer -- a legacy one-atom row does not know
        # whether it was `['X']` or `'X'` -- which is the fabrication
        # the column exists to stop. Same direction as `loss_scope`,
        # `element_tag` and `date_shifted` above: NULL is ungraded, not
        # guessed at.
        #
        # ADD COLUMN does not rebuild the UNIQUE index or
        # `idx_inst_attr_uid` and does not rewrite rows, so a tier with
        # millions of rows is not a migration cost.
        attribute_columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(instance_attributes)").fetchall()}
        if "value_count" not in attribute_columns:
            conn.execute(
                "ALTER TABLE instance_attributes ADD COLUMN value_count INTEGER")

        # `jitter_scheme` on patients, and the classification that fills
        # it. Last in this method because the predicate reads
        # `studies.date_shifted`, added above.
        patient_columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(patients)").fetchall()}
        if "jitter_scheme" not in patient_columns:
            conn.execute("ALTER TABLE patients ADD COLUMN jitter_scheme TEXT")
        SqliteStore._classify_unclassified_patients(conn)

    #: The unkeyed scheme's pseudonym, exactly: `ANON_` + 12 lowercase
    #: hex, 17 characters. A GLOB, so case-sensitive.
    _UNKEYED_PSEUDONYM_GLOB = "ANON_" + "[0-9a-f]" * 12

    @staticmethod
    def _classify_unclassified_patients(conn):
        """Fix the jitter scheme of every patient row that has none.

        A patient a release before 0.9.7 already de-identified -- its id
        has exactly the shape that release minted, or any of its dates
        was shifted -- keeps the unkeyed scheme, because a keyed offset
        would put a second offset on its dates, and its old offset is
        already readable out of every file exported for it. Every other
        patient is keyed.

        **On every open, not only when the column is added**, and only
        for NULL rows. 0.9.7 writes the column on every INSERT and never
        overwrites it, so a NULL can only come from a release before
        0.9.7 -- including one run against this store after an upgrade,
        which this also catches. A class once written is never revisited:
        after a keyed shift is saved, "has a shifted date" is true of
        keyed patients too, so re-deriving would downgrade them.

        **The id arm is the exact shape, never a prefix.** A prefix
        would class a keyed patient's 29-character pseudonym unkeyed if
        an older release ever wrote its row, and the unkeyed arm would
        then seed that patient's offset on characters of the pseudonym
        the export carries. A non-hex `ANON_` id an older release shifted
        is still caught by the two witness arms. `instr` over the
        serialized JSON finds a nested item's `__shifted__` record too,
        because nested items are serialized into the same blob.
        """
        conn.execute("""
            UPDATE patients SET jitter_scheme = CASE WHEN
                (length(patient_id) = 17 AND patient_id GLOB ?)
                OR EXISTS (SELECT 1 FROM studies s
                           WHERE s.patient_id_fk = patients.id
                             AND s.date_shifted = 1)
                OR EXISTS (SELECT 1 FROM studies s
                           JOIN series se ON se.study_id_fk = s.id
                           JOIN instances i ON i.series_id_fk = se.id
                           WHERE s.patient_id_fk = patients.id
                             AND instr(i.attributes_json, '"__shifted__"') > 0)
              THEN ? ELSE ? END
            WHERE jitter_scheme IS NULL
        """, (SqliteStore._UNKEYED_PSEUDONYM_GLOB,
              entities.JITTER_SCHEME_UNKEYED, entities.JITTER_SCHEME_KEYED))

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

    def _wire_nested_pixel_refs(self, instance, rows):
        """Restore an Instance's nested pixel references (#183).

        Shared by `load_all` and `load_patient`, which hydrate the same rows
        through two separate loops.

        **Wired unconditionally, without resolving the path against the
        graph.** A ref whose item is gone is not dropped here, because the
        export post-pass is the single place that decides carried-or-
        reported: silently discarding it at hydration would mean no
        `DATA_LOSS` row for bytes that are in the store and cannot be
        placed, which is a loss the caller cannot see -- the shape #125 and
        #169 are both about.

        **And no geometry is captured here.** These are references, not
        loaders, and the reason is in `io_handlers.NestedPixelRef`: a loader
        built now would reshape against the geometry the graph has *now*,
        and the whole point of re-checking at export is that the graph may
        have moved by then.

        Args:
            instance (Instance): The hydrated instance.
            rows: `instance_blobs` rows whose kind matches `pixels:%`, or
                None when this instance has none.
        """
        if not rows:
            return

        for row in rows:
            try:
                _root, path, terminal_tag = parse_blob_kind(row['kind'])
            except ValueError:
                # Unreachable while both write doors are gated, and handled
                # anyway: a row written by an older or hand-edited store is
                # data, not an instruction, and the reader allow-lists.
                # Skipping it leaves the bytes in the sidecar, where
                # compaction still sees them as live.
                get_logger().warning(
                    "Ignoring blob row with unreadable kind %r for %s",
                    row['kind'], instance.sop_instance_uid)
                continue
            # The provenance geometry is re-derived from the graph as
            # loaded rather than stored in a column, and the two are the
            # same answer: a saved store holds the item exactly as ingest
            # left it, so this reads what ingest recorded. It costs no
            # schema change, and a schema change here is the expensive
            # direction. A ref whose item is already gone gets None, which
            # disables only the comparison -- the export's own resolve
            # still files the loss row.
            item = resolve_item_path(instance, path)
            instance._nested_pixel_refs[(path, terminal_tag)] = NestedPixelRef(
                self.sidecar_path, row['offset'], row['length'],
                row['compress_alg'], row['hash'],
                nested_item_geometry(item.attributes)
                if item is not None else None)

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

        # Only the store that created a `:memory:` temp sidecar removes
        # it -- never a pickled clone (flag dropped on pickle) and never
        # a file-backed store (its sidecar is data, and its lock files
        # are stable paths other processes may be polling). Measured
        # before #376: the temp sidecar survived `close()` every time,
        # and with the gate and pass-lock that would be three leaked
        # files per session. `FileNotFoundError` is expected for a lock
        # file no acquisition ever created.
        if self._owns_temp_sidecar:
            for path in (self.sidecar_path, self._gate_path(),
                         self._pass_lock_path()):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass

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

    def get_audit_declines(self) -> List[tuple]:
        """Every `REMEDIATION_DECLINED` entry: a value the sweep left behind.

        Its own reader beside `get_audit_scan_gaps` and for the same
        reason: it is a different claim. A scan gap says an element could
        not be *read*; this says an element was read, a remediation was
        proposed for it, and the remediation did not run -- so the value
        is still in the graph and will reach the exported file (#301).

        No `loss_scope` and no `element_tag`. The reason lives in
        `details` prose: a `decline_reason` column would have no reader
        (the grade turns on the row existing and the report lists the
        rows), and `element_tag` is documented "for `SCAN_GAP` only" in
        three places, so borrowing it would falsify all three.

        Flushes first, like every other audit reader: a row still in the
        queue has not reached the table, so reading before the barrier
        would report a clean run over a session that had just declined.

        Returns:
            List[tuple]: (timestamp, entity_uid, details)
        """
        self.flush_audit_queue()
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT timestamp, entity_uid, details
                    FROM audit_log
                    WHERE action_type = 'REMEDIATION_DECLINED'
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
                f"dropped: {describe_exception(e)}")

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
                    # Restored, not recorded: the class was fixed when
                    # the store opened and hydration changes nothing.
                    p._jitter_scheme = (r['jitter_scheme']
                                        or entities.JITTER_SCHEME_KEYED)
                    p_map[r['id']] = p
                    patients.append(p)
                    stored_statuses.append((p, r['phi_status']))

                st_map = {}
                legacy_studies = 0
                for r in st_rows:
                    st = Study(r['study_instance_uid'], _as_loaded_date(r['study_date']))
                    # NULL (a row from before the column, #182) and 0 both
                    # read False: only a store that recorded the shift may
                    # claim one.
                    st.date_shifted = bool(r['date_shifted'])
                    # What the shift produced, or NULL for a row written
                    # before 0.9.6 -- which with the flag set means
                    # "shifted, value unknowable" and keeps the
                    # pre-0.9.6 rule for this study (#518). Assigned,
                    # not recorded through `record_date_shift`, because
                    # hydration restores a state rather than making an
                    # edit (#154).
                    st._shifted_study_date = r['shifted_study_date']
                    if st.date_shifted and st._shifted_study_date is None:
                        legacy_studies += 1
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

                # Nested pixel payloads, the same way and for the same
                # reason (#183). `LIKE` rather than `GLOB` is sound because
                # `parse_blob_kind` refuses an uppercase kind at both write
                # doors, so no `PIXELS:...` row can exist for SQLite's
                # ASCII-case-insensitive `LIKE` to match. Weaken that gate
                # and this read has to become `GLOB 'pixels:*'`.
                #
                # No index: `idx_blobs_uid_kind` is `(instance_uid, kind)`
                # and cannot serve a kind-only predicate, so this is a scan
                # exactly as the waveform pre-fetch above is -- and an index
                # whose only reader is a once-per-session prefetch is not
                # worth the write amplification on every blob row.
                nested_refs = {}
                for row in cur.execute(
                        "SELECT instance_uid, kind, offset, length,"
                        " compress_alg, hash FROM instance_blobs"
                        " WHERE kind LIKE 'pixels:%'").fetchall():
                    nested_refs.setdefault(row['instance_uid'], []).append(row)

                # The private tier, in one query for the whole store. Its
                # rows are the odd-group tags `_split_core_and_private`
                # kept out of `attributes_json`; nothing read them back
                # until #158, so `remove_private_tags=False` was honoured
                # only until the session was closed. Pre-fetched here for
                # the same reason as `wave_refs` above: per-instance would
                # be one query per instance on every session open.
                vertical_vrs = {}
                vertical = self.load_vertical_attributes_bulk(
                    conn=conn, vrs=vertical_vrs)

                se_map = {}
                # Which study each series belongs to, so the instance loop
                # below can read its parent's `date_shifted` (#510). The
                # loop is flat here, unlike `load_patient`'s, so the link
                # has to be kept rather than being in scope.
                se_study = {}
                for r in se_rows:
                    se = Series(r['series_instance_uid'], r['modality'], r['series_number'])
                    # Same rule as ingest and `load_patient`, by
                    # construction: this used to be a third hand-copied
                    # predicate, and the suite was green with the two
                    # identifying fields swapped here (#290).
                    se.equipment = Equipment.from_parts(
                        r['manufacturer'], r['model_name'], r['device_serial_number'])
                    se_map[r['id']] = se
                    if r['study_id_fk'] in st_map:
                        st_map[r['study_id_fk']].series.append(se)
                        se_study[r['id']] = st_map[r['study_id_fk']]

                legacy_instances = 0
                for r in i_rows:
                    inst = Instance(
                        r['sop_instance_uid'],
                        r['sop_class_uid'],
                        r['instance_number'],
                        file_path=r['file_path']
                    )
                    # Legacy date provenance, read off the row (#510).
                    # NULL alone only says the row predates per-value
                    # records; the **study's** flag is the only persisted
                    # evidence anywhere that a shift ever ran, because
                    # `Instance.date_shifted` never had a column. An
                    # old-store instance under an unshifted study has
                    # nothing to protect -- measured: such a date is
                    # already re-shifted by today's code -- and marking
                    # it legacy would hide a rule added in a later pass
                    # once its study is shifted under 0.9.6, which is
                    # #510 persisting for an instance with no real
                    # legacy. Studies are hydrated above, so the flag is
                    # set before this reads it; `load_patient` does the
                    # same from its nested loop.
                    parent = se_study.get(r['series_id_fk'])
                    if (r['shift_provenance'] is None
                            and parent is not None and parent.date_shifted):
                        inst._legacy_shift_provenance = True
                        legacy_instances += 1
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
                                "instance %s: %s", r['sop_instance_uid'], describe_exception(exc))

                    self._apply_vertical_attributes(
                        inst, vertical.get(r['sop_instance_uid'], {}),
                        vertical_vrs.get(r['sop_instance_uid'], {}))

                    # Wire up Sidecar Loader if present
                    if r['pixel_offset'] is not None and r['pixel_length'] is not None:

                        # We need to reshape after loading. The dimensions are in attributes.
                        # We can do this inside the lambda wrapper or a helper method.
                        # But Instance.attributes aren't populated yet!
                        # Wait, we populate attributes right after this.
                        # So the lambda calls self.instance methods? No, lambda binds early.

                        # The stored hash goes to the loader, explicitly. A
                        # loader built with none has no integrity check, and
                        # the fallback to `inst._pixel_hash` finds nothing
                        # here: hydration never sets it (and
                        # `tests/test_blob_storage.py` depends on that). So
                        # every reopened session read another frame's bytes
                        # at this offset as this instance's pixels (#436).
                        # Passed rather than set on the instance for #212's
                        # reason: an explicit hash has no ordering to get
                        # wrong. `load_patient` below does the same; change
                        # them together.
                        inst._pixel_loader = self._create_pixel_loader(
                            r['pixel_offset'], r['pixel_length'], r['compress_alg'], inst,
                            pixel_hash=r['pixel_hash'])

                    self._wire_waveform_loader(inst, wave_refs.get(r['sop_instance_uid']))
                    self._wire_nested_pixel_refs(
                        inst, nested_refs.get(r['sop_instance_uid']))

                    if r['series_id_fk'] in se_map:
                        se_map[r['series_id_fk']].instances.append(inst)

                    stored_statuses.append((inst, r['phi_status']))

            self.logger.info(f"Loaded {len(patients)} patients from {self.db_path}")
            self._report_legacy_shift_provenance(legacy_instances, legacy_studies)
            self._report_unkeyed_scheme(patients)

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
            self.logger.error(f"Failed to load PDF from DB: {describe_exception(e)}")
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
                # Same as load_all's; see the note there.
                p._jitter_scheme = (p_row['jitter_scheme']
                                    or entities.JITTER_SCHEME_KEYED)
                p_pk = p_row['id']
                stored_statuses = [(p, p_row['phi_status'])]
                # Collected during the walk and hydrated from the vertical
                # table in one pass afterwards. Unlike `load_all` this
                # filters by UID -- one patient's instances, not the whole
                # store's rows.
                hydrated_instances = []
                legacy_instances = 0
                legacy_studies = 0

                # Same one-query pre-fetch as load_all; see the note there.
                wave_refs = {
                    row['instance_uid']: row
                    for row in cur.execute(
                        "SELECT instance_uid, offset, length, compress_alg, hash"
                        " FROM instance_blobs WHERE kind = 'waveform'").fetchall()
                }

                # Nested pixel payloads, the same way and for the same
                # reason (#183). `LIKE` rather than `GLOB` is sound because
                # `parse_blob_kind` refuses an uppercase kind at both write
                # doors, so no `PIXELS:...` row can exist for SQLite's
                # ASCII-case-insensitive `LIKE` to match. Weaken that gate
                # and this read has to become `GLOB 'pixels:*'`.
                #
                # No index: `idx_blobs_uid_kind` is `(instance_uid, kind)`
                # and cannot serve a kind-only predicate, so this is a scan
                # exactly as the waveform pre-fetch above is -- and an index
                # whose only reader is a once-per-session prefetch is not
                # worth the write amplification on every blob row.
                nested_refs = {}
                for row in cur.execute(
                        "SELECT instance_uid, kind, offset, length,"
                        " compress_alg, hash FROM instance_blobs"
                        " WHERE kind LIKE 'pixels:%'").fetchall():
                    nested_refs.setdefault(row['instance_uid'], []).append(row)

                # Fetch Studies
                st_rows = cur.execute(
                    "SELECT * FROM studies WHERE patient_id_fk = ?", (p_pk,)).fetchall()
                for st_r in st_rows:
                    st = Study(st_r['study_instance_uid'],
                               _as_loaded_date(st_r['study_date']))
                    # Same NULL-reads-False rule as load_all; see the
                    # note there. (#182)
                    st.date_shifted = bool(st_r['date_shifted'])
                    # Same rule as load_all's; see the note there (#518).
                    st._shifted_study_date = st_r['shifted_study_date']
                    if st.date_shifted and st._shifted_study_date is None:
                        legacy_studies += 1
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
                        # Same rule as ingest and `load_all` (#290).
                        se.equipment = Equipment.from_parts(
                            se_r['manufacturer'], se_r['model_name'],
                            se_r['device_serial_number'])
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
                            # Same rule as load_all's: NULL provenance
                            # plus a shifted study means this row's
                            # already-shifted dates cannot be named, so
                            # it keeps the pre-0.9.6 rule for them
                            # (#510). `st` is in scope here, so no map
                            # is needed.
                            if (r['shift_provenance'] is None
                                    and st.date_shifted):
                                inst._legacy_shift_provenance = True
                                legacy_instances += 1
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
                                        r['sop_instance_uid'], describe_exception(exc))

                            # Wire up Sidecar. Duplicates load_all's loader
                            # construction below; the two have drifted apart
                            # before, so change them together.
                            if r['pixel_offset'] is not None and r['pixel_length'] is not None:
                                # With the stored hash, as `load_all` does
                                # and for its reason (#436).
                                inst._pixel_loader = self._create_pixel_loader(
                                    r['pixel_offset'], r['pixel_length'], r['compress_alg'], inst,
                                    pixel_hash=r['pixel_hash'])

                            self._wire_waveform_loader(
                                inst, wave_refs.get(r['sop_instance_uid']))
                            self._wire_nested_pixel_refs(
                                inst,
                                nested_refs.get(r['sop_instance_uid']))

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
                vertical_vrs = {}
                vertical = self.load_vertical_attributes_bulk(
                    [i.sop_instance_uid for i in hydrated_instances], conn=conn,
                    vrs=vertical_vrs)
                for inst in hydrated_instances:
                    self._apply_vertical_attributes(
                        inst, vertical.get(inst.sop_instance_uid, {}),
                        vertical_vrs.get(inst.sop_instance_uid, {}))

                for entity, stored in stored_statuses:
                    entity.record_phi_status(_phi_status_from_stored(stored))

                self._report_legacy_shift_provenance(legacy_instances,
                                                     legacy_studies)
                self._report_unkeyed_scheme([p])
                p.mark_subtree_persisted()
                return p
        except sqlite3.Error as e:
            self.logger.error(f"Failed to load patient: {describe_exception(e)}")
            return None

    #: What a load says once when it finds rows written before
    #: per-value date records existed (#510). One `WARNING` audit row and
    #: one log line per load, not per instance: a 100k-instance store
    #: would otherwise flood both channels, and the fact is about the
    #: store, not about any one row.
    #:
    #: A `WARNING` row is the right channel because `generate_report`
    #: grades a run with section-4 rows `REVIEW_REQUIRED` (#479), which
    #: is the honest grade for a session that cannot answer the question
    #: for part of its graph -- and it means the limitation reaches the
    #: compliance report rather than only a console the operator
    #: scrolled past.
    #:
    #: Three things the wording does deliberately: it names *which*
    #: guarantee is missing (#510's, not #513's), it names the failure
    #: direction (a real date may survive; a double shift can never
    #: happen), and it names the remedy.
    _LEGACY_SHIFT_NOTICE = (
        "{count} in this store were written before per-value date "
        "records existed (0.9.6). For those Isocenter cannot tell a date "
        "it already shifted from one it never touched, so it keeps the "
        "pre-0.9.6 rule for them: once the study's date is shifted, "
        "their SHIFT/JITTER values are not re-examined. The guarantee "
        "that does not apply to them is the new one -- \"a date under a "
        "SHIFT rule that this pipeline never shifted is raised and "
        "shifted\" (#510). They are never shifted twice (#513). "
        "Re-ingesting those files from source gives them the full "
        "guarantee."
    )

    def _report_legacy_shift_provenance(self, instances: int, studies: int = 0):
        """Say once that part of this graph keeps the pre-0.9.6 rule.

        One notice, not two: the instance half (#510) and the study half
        (#518) are the same limitation at two levels, and an operator
        reading two rows about one store would reasonably think there
        were two problems.

        **"Per load" is a call-site fact here, not a shape.** The owner's
        ruling is one `WARNING` row and one log line per *load*, counting
        the instances and studies affected -- never one per instance.
        Nothing in this method enforces that: it reports whatever counts
        it is handed, and `load_patient` calls it as well as `load_all`.
        On every path the public API can reach it is still one notice,
        because `Session` loads through `load_all` exactly once and never
        calls `load_patient`. A caller that loaded patients one at a time
        would get one notice each and break the ruling, so such a caller
        has to accumulate its counts and report once -- or this method
        has to learn to speak for a load rather than for a call. Said
        here because the constraint lives at the call site, where a
        future reader will not be looking.
        """
        if not instances and not studies:
            return
        parts = []
        if instances:
            parts.append(f"{instances} instance"
                         f"{'' if instances == 1 else 's'}")
        if studies:
            parts.append(f"{studies} stud{'y' if studies == 1 else 'ies'}")
        detail = self._LEGACY_SHIFT_NOTICE.format(count=" and ".join(parts))
        self.logger.warning(detail)
        self.log_audit(action_type="WARNING", entity_uid=self.db_path,
                       details=detail)

    #: What a load says once when part of the store keeps the unkeyed
    #: pseudonym and date offset of releases before 0.9.7. Its own row,
    #: not folded into `_LEGACY_SHIFT_NOTICE`: that notice is about which
    #: dates can be told apart, this one about whether they can be
    #: recovered, and one paragraph carrying both would read as one
    #: limitation. Same channel and the same "per load" call-site caveat
    #: as `_report_legacy_shift_provenance`. A sentence whose count is
    #: zero is left out.
    _UNKEYED_SCHEME_NOTICE = (
        "{n} in this store {were} de-identified before 0.9.7 under the "
        "unkeyed scheme (GHSA-phg9-vcvc-j4r7): {their} `ANON_` "
        "pseudonym{s}, where {they} {have} one, {is_} an unsalted SHA-256 of "
        "the original Patient ID, which can be reversed by trying candidate "
        "IDs, and {their} date offset can be computed from that pseudonym, "
        "or from the original Patient ID where the pseudonym was never "
        "written, by anyone who knows the date-jitter range. Isocenter "
        "keeps that scheme for them so each patient has "
        "one offset, and dates shifted for them now are recoverable in "
        "the same way.")
    _UNKEYED_PSEUDONYM_NOTICE = (
        "{m} {further}carr{ies} an unkeyed pseudonym from an export made "
        "before 0.9.7, which is exported unchanged.")
    _UNKEYED_REMEDY = (
        "Re-ingesting the source files into a new store gives them a "
        "keyed pseudonym and offset; files already exported cannot be "
        "fixed from here.")

    @classmethod
    def _unkeyed_scheme_detail(cls, legacy: int, pseudonyms: int) -> str:
        """The unkeyed-scheme notice for these counts, or `""`."""
        if not legacy and not pseudonyms:
            return ""
        parts = []
        if legacy:
            parts.append(cls._UNKEYED_SCHEME_NOTICE.format(
                n=f"{legacy} patient{'' if legacy == 1 else 's'}",
                were="was" if legacy == 1 else "were",
                their="its" if legacy == 1 else "their",
                s="" if legacy == 1 else "s",
                they="it" if legacy == 1 else "they",
                have="has" if legacy == 1 else "have",
                is_="is" if legacy == 1 else "are"))
        if pseudonyms:
            parts.append(cls._UNKEYED_PSEUDONYM_NOTICE.format(
                m=f"{pseudonyms} patient{'' if pseudonyms == 1 else 's'}",
                further="further " if legacy else "",
                ies="ies" if pseudonyms == 1 else "y"))
        parts.append(cls._UNKEYED_REMEDY)
        return " ".join(parts)

    def _report_unkeyed_scheme(self, patients):
        """Say once per load how many patients the unkeyed scheme reaches.

        Two counts: patients classed `JITTER_SCHEME_UNKEYED` (their
        offset stays recoverable), and keyed patients whose id is an
        unkeyed pseudonym carried in from a pre-0.9.7 export (an id
        already `ANON_` is never replaced, so it is exported as it is).
        """
        legacy = sum(1 for p in patients
                     if p._jitter_scheme == entities.JITTER_SCHEME_UNKEYED)
        pseudonyms = sum(1 for p in patients
                         if p._jitter_scheme != entities.JITTER_SCHEME_UNKEYED
                         and _is_unkeyed_pseudonym_shape(p.patient_id))
        detail = self._unkeyed_scheme_detail(legacy, pseudonyms)
        if not detail:
            return
        self.logger.warning(detail)
        self.log_audit(action_type="WARNING", entity_uid=self.db_path,
                       details=detail)

    # ------------------------------------------------------------------
    # The project secret
    # ------------------------------------------------------------------

    #: The secret file's first line, before the 64 hex characters. A
    #: version in the prefix so a later format is refused, not misread.
    _SECRET_FILE_PREFIX = "isocenter-project-secret-v1:"

    _MISSING_SECRET_REFUSAL = (
        "This store holds dates shifted under a project secret it no "
        "longer has ({n}). Load that secret with "
        "store_backend.load_project_secret(path) before audit() or "
        "anonymize(); generating a new one would give {those} a second "
        "date offset.")

    _FOREIGN_PSEUDONYM_NOTICE = (
        "{n} in this store carr{ies} {a}`ANON_` pseudonym{s} minted under a "
        "different project secret{generated}. {their} dates are shifted "
        "with this store's offsets, not the ones {their_lc} source store "
        "used. If this data belongs to an existing project, load that "
        "project's secret into a fresh store with "
        "store_backend.load_project_secret(path) before its first "
        "audit(), and re-ingest.")

    _LEGACY_PATIENT_NEW_DATA_NOTICE = (
        "{n} in this store {were} added under an original Patient ID whose "
        "earlier data this store de-identified before 0.9.7 under the "
        "unkeyed scheme. The new data receives a keyed pseudonym and date "
        "offset, and the earlier studies keep the unkeyed ones, so each "
        "such patient appears as two subjects whose dates carry different "
        "offsets.")

    def _read_project_secret(self, conn) -> Optional[bytes]:
        return self._read_project_secret_row(conn)[0]

    def _read_project_secret_row(self, conn):
        """`(secret, origin)`, or `(None, None)` with no row."""
        row = conn.execute(
            "SELECT secret_hex, origin FROM project_secret WHERE id = 1"
        ).fetchone()
        return (bytes.fromhex(row[0]), row[1]) if row else (None, None)

    def _patient_secret_evidence(self, conn):
        """`(patient_id, scheme, witnessed)` for every stored patient.

        `witnessed` is whether any of its dates was shifted: a study's
        `date_shifted`, or a `__shifted__` record at any depth of any of
        its instances. Ingest writes neither, so a freshly ingested
        export carries none.
        """
        return [(row[0], row[1] or entities.JITTER_SCHEME_KEYED, bool(row[2]))
                for row in conn.execute("""
            SELECT p.patient_id, p.jitter_scheme,
                   EXISTS (SELECT 1 FROM studies s
                           WHERE s.patient_id_fk = p.id AND s.date_shifted = 1)
                   OR EXISTS (SELECT 1 FROM studies s
                              JOIN series se ON se.study_id_fk = s.id
                              JOIN instances i ON i.series_id_fk = se.id
                              WHERE s.patient_id_fk = p.id
                                AND instr(i.attributes_json, '"__shifted__"') > 0)
            FROM patients p
        """).fetchall()]

    def _project_secret_for_use(self, diagnose: bool = True) -> bytes:
        """The project secret `audit()` and `anonymize()` derive under.

        Read from the store on every call and never cached, so a secret
        loaded between two calls takes effect at once and no pickle of
        this store carries it. The evidence is the store's rows, which is
        why `Session.audit()` drains the persistence manager first.

        With no secret row:

        - **Refused** (`RuntimeError`, nothing created) when a keyed
          patient has any shifted date. The row is created before the
          first keyed shift can happen, so such a patient's dates were
          shifted under a secret this store no longer has, and a new one
          would certainly give them a second offset.
        - **Generated, with a `WARNING` row**, when keyed `ANON_`
          pseudonyms are present but nothing was shifted: an export from
          another project ingested into a fresh store. The split is
          across stores, not inside this one.
        - **Generated silently** otherwise.

        With `diagnose` (what `audit()` passes; `anonymize()` does not,
        so `anonymize(audit())` writes each notice once), a `WARNING` row
        also names keyed pseudonyms that do not verify under the secret,
        ids re-ingested from a pre-0.9.7 export, and patients whose raw
        data arrived after this store de-identified them under the
        unkeyed scheme.

        Generation is `INSERT OR IGNORE` then `SELECT`, so two sessions
        reaching first use on one database at once converge on one
        secret.
        """
        with self._get_connection() as conn:
            secret, origin = self._read_project_secret_row(conn)
            evidence = self._patient_secret_evidence(conn)

        keyed = [(pid, witnessed) for pid, scheme, witnessed in evidence
                 if scheme != entities.JITTER_SCHEME_UNKEYED]
        generated_here = False
        if secret is None:
            lost = sum(1 for _pid, witnessed in keyed if witnessed)
            if lost:
                raise RuntimeError(self._MISSING_SECRET_REFUSAL.format(
                    n=f"{lost} patient{'' if lost == 1 else 's'}",
                    those="that patient" if lost == 1 else "those patients"))
            generated_here, secret = self._insert_project_secret(
                secrets.token_bytes(32), self._ORIGIN_GENERATED)

        notices = []
        if diagnose and origin == self._ORIGIN_LOADED_UNVERIFIED:
            notices.append(self._UNVERIFIED_SECRET_NOTICE)
        if diagnose or generated_here:
            foreign = sum(1 for pid, _w in keyed
                          if _is_keyed_pseudonym_shape(pid)
                          and not _pseudonym_verifies(pid, secret))
            if foreign:
                notices.append(self._FOREIGN_PSEUDONYM_NOTICE.format(
                    n=f"{foreign} patient{'' if foreign == 1 else 's'}",
                    ies="ies" if foreign == 1 else "y",
                    a="an " if foreign == 1 else "",
                    s="" if foreign == 1 else "s",
                    generated=(", and this store had none of its own, so a "
                               "new one was generated" if generated_here
                               else ""),
                    their="Its" if foreign == 1 else "Their",
                    their_lc="its" if foreign == 1 else "their"))
        if diagnose:
            legacy_ids = {pid for pid, scheme, _w in evidence
                          if scheme == entities.JITTER_SCHEME_UNKEYED}
            returned = sum(
                1 for pid, _w in keyed
                if legacy_ids and not _is_replacement_id(pid)
                and _unkeyed_replacement_id_for(pid) in legacy_ids)
            if returned:
                notices.append(self._LEGACY_PATIENT_NEW_DATA_NOTICE.format(
                    n=f"{returned} patient{'' if returned == 1 else 's'}",
                    were="was" if returned == 1 else "were"))
            carried = sum(1 for pid, _w in keyed
                          if _is_unkeyed_pseudonym_shape(pid))
            if carried:
                notices.append(self._unkeyed_scheme_detail(0, carried))
        if notices:
            detail = " ".join(notices)
            self.logger.warning(detail)
            self.log_audit(action_type="WARNING", entity_uid=self.db_path,
                           details=detail)
        return secret

    def _insert_project_secret(self, secret: bytes, origin: str):
        """Insert `secret` unless a row exists; return `(inserted, row)`.

        `row` is whatever the store holds afterwards, which is the
        winner when another session inserted first. Never `REPLACE`: a
        replaced secret is a second offset for every patient already
        shifted under the first.
        """
        with self._get_connection() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO project_secret "
                "(id, secret_hex, origin, created_at) VALUES (1, ?, ?, ?)",
                (secret.hex(), origin, datetime.now().isoformat()))
            inserted = cur.rowcount == 1
            return inserted, self._read_project_secret(conn)

    def write_project_secret(self, path: str) -> None:
        """Write this store's project secret to a new file at `path`.

        The carry for keeping offsets consistent across stores: write it
        here, then `load_project_secret(path)` on the other store before
        its first `audit()`. A store with no secret yet gets one first,
        under the same rules `audit()` applies.

        The file is created with `O_EXCL` and mode `0o600`, and holds the
        secret that recovers the dates of every store sharing it: treat
        it as you treat the store.

        Raises:
            FileExistsError: `path` exists. Never overwritten: a secret
                written over another is a project lost.
            RuntimeError: As `audit()`, for a store holding dates shifted
                under a secret it no longer has.
        """
        if os.path.lexists(path):
            raise FileExistsError(
                f"{path} exists; write_project_secret never overwrites a "
                "file")
        secret = self._project_secret_for_use(diagnose=False)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(f"{self._SECRET_FILE_PREFIX}{secret.hex()}\n")

    @classmethod
    def _parse_secret_file(cls, path: str) -> bytes:
        with open(path, "r", encoding="ascii", errors="replace") as handle:
            text = handle.read()
        body = text[:-1] if text.endswith("\n") else text
        hex_part = body[len(cls._SECRET_FILE_PREFIX):]
        if (not body.startswith(cls._SECRET_FILE_PREFIX)
                or len(hex_part) != 64
                or any(c not in "0123456789abcdef" for c in hex_part)):
            # The content is deliberately not quoted: it may be a secret.
            raise ValueError(
                f"{path} is not a project secret file (expected one line: "
                f"{cls._SECRET_FILE_PREFIX} followed by 64 lowercase hex "
                "characters)")
        return bytes.fromhex(hex_part)

    def load_project_secret(self, path: str) -> None:
        """Adopt the project secret in `path` for this store.

        Only for a store with no secret of its own: load it before the
        store's first `audit()` or `anonymize()`, either of which creates
        one. A store holding keyed pseudonyms accepts only a secret that
        minted at least one of them. A store holding shifted dates but no
        keyed pseudonym (its patients kept their Patient IDs) has nothing
        to check a secret against: the secret is accepted, recorded as
        unverified, and a `WARNING` row says so at the load and at every
        later `audit()`, so the report grades `REVIEW_REQUIRED`.

        Raises:
            FileNotFoundError: `path` does not exist.
            ValueError: `path` is not a project secret file, or the store
                holds keyed pseudonyms and none of them was minted under
                this secret.
            RuntimeError: The store already holds a project secret --
                always, even the same one. Anything it pseudonymized or
                shifted was derived under that secret, and adopting
                another would give those patients a second offset.
        """
        secret = self._parse_secret_file(path)
        with self._get_connection() as conn:
            existing = self._read_project_secret(conn)
            evidence = self._patient_secret_evidence(conn)
        if existing is not None:
            raise RuntimeError(self._SECRET_ALREADY_HELD)
        keyed_pseudonyms = [
            pid for pid, scheme, _w in evidence
            if scheme != entities.JITTER_SCHEME_UNKEYED
            and _is_keyed_pseudonym_shape(pid)]
        if keyed_pseudonyms and not any(
                _pseudonym_verifies(pid, secret) for pid in keyed_pseudonyms):
            raise ValueError(
                f"this secret did not mint any pseudonym in this store "
                f"({len(keyed_pseudonyms)} keyed pseudonym"
                f"{'' if len(keyed_pseudonyms) == 1 else 's'} checked); "
                "it belongs to a different project")
        # Shifted patients with nothing to check this secret against: a
        # store whose patients kept their Patient IDs. Refusing would
        # leave such a store no way back after its secret row is lost, so
        # the load is accepted and recorded as unverified, and says so now
        # and at every later audit(): if this is not the secret those
        # dates were shifted under, the next date shifted for them takes a
        # second offset, and nothing left in the store can tell.
        unverifiable = 0
        if not keyed_pseudonyms:
            unverifiable = sum(
                1 for _pid, scheme, witnessed in evidence
                if witnessed and scheme != entities.JITTER_SCHEME_UNKEYED)
        # Once unverified, always unverified. A store that took a secret
        # unverified and then minted pseudonyms under it will verify that
        # same secret on any later load -- after its row is lost again,
        # say -- but the verification only shows the secret matches what
        # was derived *after* the unverified load. The dates shifted
        # before it were shifted under the secret that was lost, and a
        # verifying pseudonym says nothing about them. So a verified load
        # is not a reason to stop warning, and the fact is read from the
        # audit log, which the project_secret row being lost does not
        # take with it.
        was_unverified = (not unverifiable
                          and self._store_recorded_an_unverified_secret())
        origin = (self._ORIGIN_LOADED_UNVERIFIED
                  if unverifiable or was_unverified else self._ORIGIN_LOADED)
        inserted, _row = self._insert_project_secret(secret, origin)
        if not inserted:
            raise RuntimeError(self._SECRET_ALREADY_HELD)
        if unverifiable or was_unverified:
            if unverifiable:
                detail = self._UNVERIFIED_LOAD_NOTICE.format(
                    n=f"{unverifiable} patient{'' if unverifiable == 1 else 's'}",
                    has="has" if unverifiable == 1 else "have",
                    those="that patient" if unverifiable == 1 else "those patients")
            else:
                detail = self._UNVERIFIED_HISTORY_NOTICE
            detail += " " + self._UNVERIFIED_SECRET_NOTICE
            self.logger.warning(detail)
            self.log_audit(action_type="WARNING", entity_uid=self.db_path,
                           details=detail)

    def _store_recorded_an_unverified_secret(self) -> bool:
        """Whether any `WARNING` row says a secret was loaded unverified."""
        self.flush_audit_queue()
        with self._get_connection() as conn:
            return conn.execute(
                "SELECT 1 FROM audit_log WHERE action_type = 'WARNING' "
                "AND instr(details, ?) > 0 LIMIT 1",
                (self._UNVERIFIED_MARKER,)).fetchone() is not None

    #: `project_secret.origin` values. A store format, like the scheme
    #: names: `_project_secret_for_use` reads the unverified one back at
    #: every `audit()`, because after the load nothing else in the store
    #: records that the secret was never checked.
    _ORIGIN_GENERATED = "generated"
    _ORIGIN_LOADED = "loaded"
    _ORIGIN_LOADED_UNVERIFIED = "loaded-unverified"

    _UNVERIFIED_LOAD_NOTICE = (
        "{n} in this store {has} dates shifted under a project secret and "
        "no keyed `ANON_` pseudonym to check a secret against, so the "
        "secret just loaded was accepted without verification.")
    _UNVERIFIED_HISTORY_NOTICE = (
        "This store's audit log records a project secret loaded earlier "
        "without verification. The secret just loaded verifies against "
        "the store's keyed pseudonyms, but those can have been minted "
        "under that unverified secret, so the check says nothing about "
        "the dates shifted before it.")

    #: The words `_store_recorded_an_unverified_secret` finds in the audit
    #: log. A store format: stores written since 0.9.7 carry them in their
    #: WARNING rows, so rewording `_UNVERIFIED_SECRET_NOTICE` without
    #: keeping this phrase forgets every unverified load already recorded.
    _UNVERIFIED_MARKER = "project secret could not be verified when it was loaded"

    _UNVERIFIED_SECRET_NOTICE = (
        "This store's project secret could not be verified when it was "
        "loaded. If it is not the secret this store's earlier dates were "
        "shifted under, every date shifted since the load carries a "
        "different offset from the dates of the same patients shifted "
        "before it, and the store cannot tell which. Confirm the secret "
        "file came from this store's own project.")

    _SECRET_ALREADY_HELD = (
        "load_project_secret() refused: this store already holds a project "
        "secret, and everything it pseudonymized or shifted was derived "
        "under it. Load a project's secret into a fresh store, before its "
        "first audit() or anonymize().")

    def _serialize_item(self, item: Instance) -> Dict[str, Any]:
        """
        Serializes a DicomItem (or Instance) to a dictionary, including attributes and sequences.
        """
        data = item.attributes.copy()
        # `__shifted__` here as well as in `_serialize_dicom_item`, and
        # unlike `__vrs__`, which the root deliberately omits (#510,
        # #513). A root private tag's VR has a storage home of its own in
        # `value_rep`, so a copy here would be a second answer; a date
        # record has no other home at any depth, so the root needs this
        # key or a top-level shifted date is raised and shifted again on
        # the next load.
        if getattr(item, "_shifted_dates", None):
            data['__shifted__'] = dict(item._shifted_dates)
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
        """Helper for recursive serialization of generic DicomItems.

        Carries `__vrs__` alongside `__sequences__` (#154). A nested
        private tag never reaches the `instance_attributes` table -- it
        rides this JSON -- so `value_rep`, the storage home the top-level
        carrier uses, does not exist for it. Without this key an inner
        private tag would behave differently from an outer one on the
        very same instance.

        `_serialize_item`, the root, deliberately emits **no** top-level
        `__vrs__`: a root private tag's VR lives in `value_rep`, and a
        second copy here would be a second answer that can disagree with
        it after a partial write.
        """
        data = item.attributes.copy()
        if getattr(item, "attribute_vrs", None):
            data['__vrs__'] = dict(item.attribute_vrs)
        # The per-value date record, the same way (#513). A nested date
        # is the half `Instance.date_shifted` could never speak for, so
        # without this key it comes back from the store unvouched-for and
        # the next pass shifts it again.
        if getattr(item, "_shifted_dates", None):
            data['__shifted__'] = dict(item._shifted_dates)
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
        vrs_data = data.pop('__vrs__', None)
        # Popped **before** `attributes.update(data)` below, exactly as
        # `__vrs__` is: left in, the key would land in `attributes` as a
        # tag that is not a tag, and reach every reader of it -- the
        # exporter's merge and `export_dataframe(expand_metadata=True)`
        # among them (#510, #513).
        shifted_data = data.pop('__shifted__', None)

        # 1. Attributes
        target_item.attributes.update(data)
        if vrs_data:
            # Assigned, not recorded through `record_attr_vr`, for the
            # same reason the attributes above are: hydration restores a
            # state, it does not make an edit (#154).
            target_item.attribute_vrs.update(vrs_data)
        if shifted_data:
            # Assigned rather than recorded through `record_date_shift`,
            # for the same reason (#154): hydration restores a state.
            target_item._shifted_dates = dict(shifted_data)

        # 2. Sequences
        if sequences_data:
            from .entities import DicomItem
            for tag, items_list in sequences_data.items():
                # Before the item loop, and unconditional. `_serialize_item`
                # already stores a zero-item sequence as `{"0009,1005": []}`,
                # but iterating an empty list called `add_sequence_item`
                # zero times, so hydration dropped what the store had
                # faithfully kept: a graph that ingested an empty `SQ`
                # exported it, and the same graph after a save/close/reopen
                # did not (#392).
                target_item.add_sequence(tag)
                for item_data in items_list:
                    new_item = DicomItem()
                    self._deserialize_into(new_item, item_data)
                    target_item.add_sequence_item(tag, new_item)

    def save_vertical_attributes(
            self, instance_uid: str, attributes: Dict[Tuple[str, str], Any],
            conn: sqlite3.Connection = None,
            vrs: Dict[Tuple[str, str], str] = None):
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

        `value_text` is NULL for exactly one thing: an atom whose value
        was `None` (#339). Every other rendering goes through `str()`,
        which never returns `None`, so no row written by any released
        version can hold a NULL there and the meaning needs no
        migration to claim.

        `value_count` carries the container's length on every row of an
        element (#328), denormalized per atom exactly as `value_rep` is,
        and `NULL` for a value that was not a container at all. An empty
        container has no atom to hang it on, so it writes one
        **placeholder row** -- atom 0, `value_text` NULL, `value_count`
        0 -- whose atom the read side discards unread.

        Args:
            instance_uid (str): The SOP Instance UID.
            attributes (Dict[Tuple[str, str], Any]): Mapping of (Group, Element) hex strings to values.
            conn (sqlite3.Connection, optional): An existing database connection to use for the transaction.
            vrs (Dict[Tuple[str, str], str], optional): The source VR for
                each tag, keyed the same way. `value_rep` was reserved for
                this and hardcoded to `"UN"` until #154; a tag with no
                entry still stores `"UN"`, which is the honest answer for
                a value whose VR was never known.
        """
        data_rows = []
        for (grp, elem), val in attributes.items():
            # `"UN"` remains the default, and it is not a placeholder any
            # more: it says this value's VR was never recorded, which is
            # what an Implicit VR source produces for every private
            # element. `load_vertical_attributes_bulk` reads it back and
            # `_value_fits_vr` refuses it, so such a value takes the
            # export fallback exactly as it did before (#154).
            vr = (vrs or {}).get((grp, elem), "UN")
            # Check for VM > 1. `MultiValue` is what pydicom hands back for a
            # multi-valued element and it is a MutableSequence, NOT a list, so
            # a bare `isinstance(val, list)` sent it down the scalar arm and
            # stored "['a', 'b', 'c']" in one row -- a string that reloads
            # looking like a list. `IsocenterJSONEncoder` unwraps MultiValue
            # for the other tier for the same reason. `tuple` is here
            # because `_merge` names it: a `()` that took the scalar arm
            # was stored as the text `'()'` and reloaded as a value the
            # source never had (found by review of #391, #367).
            if isinstance(val, (list, tuple, MultiValue)):
                if not val:
                    # The placeholder row for an empty container (#328).
                    # There is no atom, so `value_text` is NULL and the
                    # read side throws the atom away without looking at
                    # it -- the row exists only to carry the `0`. A
                    # container with no values still has to write
                    # something, or "the tag was present and empty" and
                    # "the tag was not there" are the same absence of
                    # rows, which is exactly what they were.
                    data_rows.append(
                        (instance_uid, grp, elem, 0, vr, None, 0))
                    continue
                for idx, atom in enumerate(val):
                    data_rows.append((instance_uid, grp, elem, idx, vr,
                                      _vertical_atom_text(vr, atom), len(val)))
            else:
                # NULL, not `1`. A scalar is not a container of one, and
                # storing `1` here would make a value that was written
                # as `'X'` reload as `['X']`.
                data_rows.append((instance_uid, grp, elem, 0, vr,
                                  _vertical_atom_text(vr, val), None))

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
                        INSERT INTO instance_attributes (instance_uid, group_id, element_id, atom_index, value_rep, value_text, value_count)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
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
            self.logger.error(f"Failed to save vertical attributes for {instance_uid}: {describe_exception(e)}")
            raise

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
            conn: sqlite3.Connection = None,
            vrs: Optional[Dict[str, Dict[Tuple[str, str], str]]] = None
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
        for VM > 1 -- **except** where `value_rep` names a VR whose values
        are not text on the wire. `US`, `UL`, `FL` and their siblings come
        back as numbers, because pydicom refuses a `str` for them at write
        time and would fail the whole export rather than the element
        (`_VERTICAL_VR_PARSERS`). Nothing is inferred from the text: the
        VR the source file gave decides, and a tag stored under `"UN"` --
        which is every private element of an Implicit VR source -- still
        reloads as text (#154).

        Arity comes back too, since #328. `value_count` is read off the
        first row of an element exactly as `value_rep` is: `0` over the
        placeholder row is an empty container, `n >= 1` is a list even
        at `n == 1`, and NULL
        is either a scalar or a row written before the column existed --
        both of which take the old rule, "more than one atom is a list".
        The column is never used to truncate or pad, in either
        direction: the rows decide the values, and a stored count that
        disagrees with them is a corrupt row, not a licence to drop
        values that are sitting in the table. That holds at `0` too --
        the empty container is recognised by the placeholder row's shape
        (one atom, NULL text) and not by the count alone, so a `0`
        written over real atoms hands them back rather than swallowing
        them.

        An atom stored as a NULL `value_text` comes back as `None`, not
        as the text `None` (#339). The export then reports it as data
        loss exactly as the in-memory path does, rather than writing a
        conformant-looking `LO` element carrying a word the source never
        said.

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
            vrs (Optional[Dict]): An accumulator, filled with SOP Instance
                UID -> {(group, element): value_rep} when given. An
                out-parameter rather than a second return value, matching
                `populate_attrs`' `dropped`/`unscanned` and `_merge`'s
                `losses`: every existing caller reads the return
                positionally, and widening it to a `(value, vr)` pair
                would rewrite all of them for one caller's benefit.
                `"UN"` entries are included -- "no VR was recorded" is an
                answer, and dropping it would be indistinguishable from
                "this method was not asked".

        Returns:
            Dict[str, Dict[Tuple[str, str], Any]]: SOP Instance UID ->
            {(group, element): value}. Instances with no vertical rows are
            absent rather than present-and-empty.
        """
        select = ("SELECT instance_uid, group_id, element_id, value_rep,"
                  " value_text, value_count FROM instance_attributes")
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
        atoms: Dict[Tuple[str, str, str], List[Any]] = {}
        reps: Dict[Tuple[str, str, str], str] = {}
        counts: Dict[Tuple[str, str, str], Optional[int]] = {}
        ctx = self._get_connection() if conn is None else nullcontext(conn)
        with ctx as db:
            for sql, params in queries:
                # Iterated, not fetchall()'d: the rows are turned into the
                # grouped result as they arrive rather than held twice,
                # which matters when the filter is None and the table
                # covers the whole store.
                for row in db.execute(sql, params):
                    key = (row['instance_uid'], row['group_id'], row['element_id'])
                    # The stored VR of the FIRST atom stands for the
                    # element: `save_vertical_attributes` writes one VR
                    # per element across every atom, so a disagreement
                    # here would be a corrupt row rather than a case to
                    # reconcile.
                    rep = row['value_rep'] or "UN"
                    reps.setdefault(key, rep)
                    # `value_count` is read off the first row for the
                    # same reason `value_rep` is: it is written per
                    # element across every atom.
                    counts.setdefault(key, row['value_count'])
                    atoms.setdefault(key, []).append(
                        _vertical_atom_value(rep, row['value_text']))

        results: Dict[str, Dict[Tuple[str, str], Any]] = {}
        for (uid, grp, elem), values in atoms.items():
            count = counts[(uid, grp, elem)]
            # `count == 0`, never `not count`: `None` is falsey and means
            # "no arity was recorded", which is a scalar or a legacy row
            # -- reading it as an empty container would turn every one of
            # them into `[]`. And the count is asked BEFORE `value_text`
            # is consulted, which is the other half of the same trap:
            # the empty container's placeholder row and a real `None`
            # atom (#339) both carry a NULL `value_text` and differ only
            # here, so a reader that looked at the text first would
            # reload `[]` as `[None]`.
            #
            # `values == [None]` is the placeholder row's exact shape --
            # one atom, NULL text -- and no writer produces `0` over any
            # other. Checking it costs one comparison and buys the
            # no-truncation rule without an exception: a hand-edited
            # store carrying `0` over three real atoms hands back the
            # three, where a bare `count == 0` silently returned `[]`
            # and dropped values that were sitting in the table.
            if count == 0 and values == [None]:
                value = []
            elif count is None:
                # No recorded arity: the pre-#328 rule, which is what a
                # scalar wants and the only honest answer for a row
                # written before the column existed.
                value = values if len(values) > 1 else values[0]
            else:
                # A container, even at one value. `list(values)` rather
                # than a slice to `count`: the rows are the values, and
                # a count that disagrees with them is a corrupt row, not
                # a licence to drop values that are in the table.
                value = list(values)
            results.setdefault(uid, {})[(grp, elem)] = value
            if vrs is not None:
                vrs.setdefault(uid, {})[(grp, elem)] = reps[(uid, grp, elem)]
        return results

    @staticmethod
    def _apply_vertical_attributes(instance: Instance,
                                   private: Dict[Tuple[str, str], Any],
                                   vrs: Dict[Tuple[str, str], str] = None) -> None:
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

        # Same rule for the VR carrier, and for the same reason:
        # assigned, never recorded through `record_attr_vr`. `"UN"` is
        # skipped rather than stored -- it means no VR was ever known,
        # and a carrier entry saying "unknown" is not the same thing as
        # no entry, which is what `_merge` reads (#154).
        for (grp, elem), vr in (vrs or {}).items():
            if vr and vr != "UN":
                instance.attribute_vrs[f"{grp},{elem}"] = vr

    def persist_blob(self, instance, kind: str, data) -> None:
        """Write a binary blob to the sidecar and record its reference.

        Args:
            instance (Instance): Owning instance.
            kind (str): A blob kind -- `'pixels'`, `'waveform'`, or either
                followed by a sequence path. See `parse_blob_kind`.
            data (bytes | np.ndarray): Payload. Arrays are passed to the
                sidecar directly to avoid a full copy.

        Raises:
            ValueError: If `kind` does not match the blob-kind grammar. This
                was a two-literal tuple until #183; the message now carries
                the grammar, because the failure it most often means is a
                caller who took the spelling from #183's `pixels:seq:...`
                sketch rather than from `serialize_blob_kind`.
        """
        import hashlib

        parse_blob_kind(kind)

        if data is None:
            return

        raw = data.tobytes() if hasattr(data, "tobytes") else data
        digest = hashlib.sha256(raw).hexdigest()

        c_alg = 'zlib'
        # Site 4 of six. The gate spans the append AND the row commit:
        # a row committed outside it can land after `_apply_new_offsets`
        # for a frame appended before `_read_blob_index`, pointing into
        # the pre-compaction layout of a smaller file (#368).
        with self._hold_sidecar_gate():
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
            kind (str): A blob kind -- see `parse_blob_kind`.
            offset (int): Byte offset of the blob within the sidecar.
            length (int): On-disk (post-compression) length in bytes.
            blob_hash (str): SHA-256 of the raw (uncompressed) payload.
            compress_alg (str): Compression used, e.g. 'zlib' or 'raw'.
            conn (sqlite3.Connection, optional): An existing connection to
                enlist in. When given, no new connection is opened and the
                write joins the caller's transaction.

        Raises:
            ValueError: If `kind` does not match the blob-kind grammar, or if
                exactly one of `offset`/`length` is None. A half-specified
                reference is never recoverable: it would pair a real offset
                with a missing or stale length. Callers with nothing to
                record must skip the call, not pass NULLs.
        """
        # Both doors, one answer (#183 Q8). `persist_blob` validated and this
        # did not, so an arbitrary string reached the table through the
        # second door -- and this is the door the *ingest* path uses, because
        # it writes its own frames through `SidecarManager` and registers the
        # reference separately. A gate on one of two doors is not a gate.
        #
        # It is also what makes the `LIKE 'pixels:%'` hydration prefetch
        # sound: SQLite's `LIKE` is ASCII case-insensitive, so a `PIXELS:...`
        # row would match it, and the grammar's lowercase-only rule is the
        # thing that makes such a row unwritable. Weakening this gate means
        # changing that read to `GLOB`.
        parse_blob_kind(kind)

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

    def get_nested_pixel_refs(self) -> Dict[Tuple[str, str], Tuple[int, int]]:
        """Every nested pixel reference, keyed `(instance_uid, kind)` (#183).

        A separate method rather than a prefix flag on `get_blob_refs`,
        because it answers a different question and returns a different
        shape. `get_blob_refs` is keyed by UID alone, which is exactly what
        a nested payload cannot be: one instance carries a bare `pixels`
        blob *and* one row per icon, and collapsing them onto a UID is how
        `compact_sidecar`'s uid_map would hand a pixel loader a thumbnail.

        Read for the same reason the waveform refs are: `compact_sidecar`'s
        uid_map is pixels-only by design, so a nested loader left on a
        pre-compaction offset reads the wrong bytes or runs off the end of
        the file.

        Returns:
            Dict[Tuple[str, str], Tuple[int, int]]: `(uid, kind)` ->
            `(offset, length)`, for rows that have both.
        """
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT instance_uid, kind, offset, length
                FROM instance_blobs
                WHERE kind LIKE 'pixels:%'
                  AND offset IS NOT NULL AND length IS NOT NULL
            """).fetchall()

        return {(r["instance_uid"], r["kind"]): (r["offset"], r["length"])
                for r in rows}

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
            # Site 5 of six. The gate is taken OUTSIDE `_pixel_swap_lock`
            # (order: gate -> swap lock, the order `_rewire_sidecar_loaders`
            # needs) and spans the append below AND the `record_blob_ref`
            # after the swap lock is released (#368). Inside this `try`
            # so a gate expiry gets the same log line as any other swap
            # failure before it becomes a `RedactionOutcome(ok=False)`.
            with self._hold_sidecar_gate():
                self._swap_pixels_under_gate(instance)
        except Exception as e:
            self.logger.error(f"Failed to persist pixel swap for {instance.sop_instance_uid}: {describe_exception(e)}")
            raise

    def _swap_pixels_under_gate(self, instance: Instance):
        """`persist_pixel_data`'s body; the caller holds the sidecar gate."""
        # The read -> sidecar write -> loader/hash rebind must be one
        # critical section against `_persist_pixels`: a background
        # save that reads the bytes before this redaction swap zeroes
        # them, and rebinds after it, leaves the instance reading
        # back its pre-redaction pixels under a redaction attestation
        # (#274). Released before `record_blob_ref` below -- never
        # hold a thread lock across a sqlite write that can wait out
        # the busy timeout. (The gate, held by the caller, is the
        # one deliberate exception to that rule, and the reason is
        # at site 4.)
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
            # The revision beside the read, for the publish below to check
            # with the identity. `set_pixel_data()` keeps a native-order
            # array as given, so a caller can edit the resident array in
            # place and set the same object again: landing after the
            # write, that set passes an identity check alone while the
            # sidecar holds the bytes from before the edit, and the flag
            # cleared over them lets an unload drop the only copy (#293's
            # shape; review of #466). Read without the leaf: a set caught
            # half-way reads as a moved revision, so the publish leaves
            # the flag set -- the safe direction, and the next save's
            # dedup clears it.
            read_revision = instance._revision

            # Hash Update (CRITICAL for Integrity Checks)
            # Calculate Hash BEFORE writing/compression to ensure we
            # capture the state exactly as it goes into the pipe.
            import hashlib
            # Ensure we are hashing the contiguous bytes
            if hasattr(b_data, 'tobytes'):
                p_hash = hashlib.sha256(b_data.tobytes()).hexdigest()
            else:
                p_hash = hashlib.sha256(b_data).hexdigest()

            # `_pixel_hash` is NOT assigned here. It names the frame the
            # loader reads, so it moves with the loader, below, after the
            # write has succeeded. Assigned here, a `write_frame` that
            # raised (a full disk, an EIO) left the instance holding the
            # hash of a frame that was never written beside a loader
            # still on the original; the next save's `arr is None` arm
            # stored that hash with the original's offset, and a reopened
            # session -- which checks it since #436 -- refused a correct
            # frame as a hash mismatch.

            # Determine suitable compression? Defaulting to zlib for
            # swap. Ideally we respect original or config, but for
            # swap zlib is safe/fast enough.
            c_alg = 'zlib'

            offset, length = self.sidecar.write_frame(b_data, c_alg)

            # 2. Update Instance Loader
            # This allows instance.unload_pixel_data() to work safely
            # Note: instance attributes ARE populated here (it's a
            # live object), so passing instance=instance works.
            swapped = self._create_pixel_loader(
                offset, length, c_alg, instance, pixel_hash=p_hash)
            # Under the pixel-state leaf, so a `set_pixel_data()` or
            # `discard_pixel_data()` on another thread lands wholly
            # before or after this publish (#434, Q6). The loader is
            # rebound either way: this is the redaction swap, and leaving
            # the instance on the pre-redaction frame is #274.
            with entities.PIXEL_STATE_LOCK:
                instance._pixel_loader = swapped
                instance._pixel_hash = p_hash
                # Only if the array written is still the one resident,
                # at the revision it was read at. A set that landed since
                # holds newer, unwritten pixels -- a new array, or this
                # one edited in place and set again -- and clearing the
                # flag would let an unload drop them (#293); a discard
                # since already cleared the record.
                if (instance.pixel_array is b_data
                        and instance._revision == read_revision):
                    # The loader now points at the bytes that are
                    # resident, so the array is recoverable and freeable
                    # again (#293).
                    instance._pixel_array_unwritten = False
                    # And they are the stored frame now, which the
                    # current descriptors describe: nothing is left for a
                    # discard to undo (#434).
                    instance._pixel_descriptors_replaced = None

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
        _warn_on_shared_patient_ids(self.logger, patients)
        # Instances are marked clean only after the commit returns. Doing it
        # inside the walk -- as this method used to -- means a rolled-back
        # save leaves memory believing it was written, so the retry skips
        # exactly the rows that failed. These are references to objects that
        # are already resident, so holding them costs a pointer each.
        saved_instances = []

        # Site 6 of six. The gate spans `_prepare_pixel_frames` AND the
        # transaction's commit -- the whole of what follows up to the
        # `mark_persisted` loop. Phase E of
        # `tests/test_compaction_races_a_concurrent_write.py` is a row
        # committed after `_apply_new_offsets` from a frame appended
        # before `_read_blob_index`; a gate released between the prepass
        # and the commit reopens it exactly (#368). The prepass takes
        # `_pixel_swap_lock` per instance under this gate, which is the
        # order the rewire needs. `compact()` takes this same gate only
        # after its leading `save(sync=True)` has returned, or it would
        # deadlock here against itself.
        with self._hold_sidecar_gate():
            # Every sidecar frame this save will need is appended HERE,
            # before any connection exists. It used to happen inside the
            # walk below, which meant the SQLite write lock was held for
            # as long as the save's whole dirty resident pixel payload
            # took to compress and write -- so a slow-storage save could
            # outlast `_SQLITE_BUSY_TIMEOUT_S` and surface in a healthy
            # concurrent writer as `database is locked` (#287). The
            # transaction below now contains row upserts and nothing else.
            prepared = self._prepare_pixel_frames(patients, tally)

            try:
                with self._get_connection() as conn:
                    cur = conn.cursor()
                    pending_deletions = []
                    for patient in patients:
                        saved_instances.extend(
                            self._save_patient(conn, cur, patient, tally,
                                               pending_deletions, prepared))

                    # Every upsert in the whole save has now run, so a
                    # child that moved to a different parent already
                    # points at it and a scoped delete will not mistake
                    # it for a removal (#77).
                    #
                    # And no scoped delete removes a row some object in
                    # `patients` still holds (#548). Two parent objects
                    # can share one row -- two `Patient`s with one
                    # `patient_id` resolve to one `patients` row -- and a
                    # delete scoped to one object's list then removes the
                    # children the other object holds. Built HERE, inside
                    # the transaction and after the walk, not beside the
                    # prepass: redaction renames `sop_instance_uid` in
                    # place under no lock this save takes, and a set
                    # frozen at the prepass would keep the old UID's row
                    # beside the renamed one's
                    # (`test_an_instance_renamed_after_the_prepass_is_still_written`).
                    held = _held_uids(patients)
                    for delete, parent, parent_pk in pending_deletions:
                        delete(cur, parent, parent_pk, held)

                    if prune_absent_patients:
                        self._delete_absent_patients(cur, patients)

                    conn.commit()
            except Exception:
                # No rollback here: `_get_connection` owns the transaction
                # and has already rolled it back and closed the connection
                # by the time this runs. Calling `conn.rollback()` on the
                # closed handle raised `ProgrammingError: Cannot operate
                # on a closed database`, which then replaced the real
                # exception -- every distinct save failure surfaced under
                # one misleading name.
                self.logger.error(
                    "Save failed; the transaction was rolled back and "
                    "nothing was marked clean", exc_info=True)
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

        # The study-level sibling of the two re-parentings below, and the
        # one that matters most: a patient's ID is exactly what
        # de-identification replaces, so after a reload every Patient ID
        # replacement writes a NEW patient row, and a study the pass did
        # not change is never upserted onto it. Its key would go on naming
        # the old row, which `_delete_absent_patients` deletes with the
        # study beneath it (#551). A merge that moves a clean study to the
        # patient already holding its new ID depends on this too (#548).
        self._reparent_studies(cur, patient, patient_pk)

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
        """Writes the patient row if dirty or missing; returns its primary key.

        **Or missing** (#552): a patient with no row under its ID is not
        persisted, whatever its revision says. `Patient` tracks no
        attribute assignment, so `patient.patient_id = ...` -- in user
        code, or in `recover_patient_identity` before it recorded the
        change -- left a clean patient under an ID with no row. The save
        then wrote nothing, found no key, skipped the whole subtree, and
        the prune deleted the old ID's row with every study beneath it.
        This reads the store rather than the bookkeeping, so it moves no
        revision and needs no setter. On the ordinary paths it never
        fires: an ingested patient is dirty until its first save, and a
        hydrated one was read from its row.
        """
        existing = cur.execute(
            "SELECT id FROM patients WHERE patient_id=?",
            (patient.patient_id,)).fetchone()
        if patient.has_unsaved_changes or existing is None:
            # `jitter_scheme` is written on every INSERT, so a NULL can
            # only come from a release before 0.9.7, and never
            # overwritten on conflict: a patient's class is fixed once,
            # and a save must not reclassify it.
            cur.execute("""
                INSERT INTO patients (patient_id, patient_name, phi_status,
                                      jitter_scheme)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(patient_id) DO UPDATE SET
                    patient_name=excluded.patient_name,
                    phi_status=excluded.phi_status,
                    jitter_scheme=COALESCE(patients.jitter_scheme,
                                           excluded.jitter_scheme)
            """, (patient.patient_id, patient.patient_name,
                  patient.phi_status.value, patient._jitter_scheme))
            tally.patients += 1
        else:
            return existing[0]

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
                INSERT INTO studies (patient_id_fk, study_instance_uid, study_date, date_shifted,
                                     shifted_study_date, phi_status)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(study_instance_uid) DO UPDATE SET
                    study_date=excluded.study_date,
                    date_shifted=excluded.date_shifted,
                    shifted_study_date=excluded.shifted_study_date,
                    patient_id_fk=excluded.patient_id_fk,
                    phi_status=excluded.phi_status
            """, (patient_pk, study.study_instance_uid,
                  _as_stored_date(study.study_date),
                  1 if study.date_shifted else 0,
                  # Plain assignment, not COALESCE-guarded, for
                  # `shift_provenance`'s reason (#518): a study whose
                  # record is None has to be able to write that None, or
                  # a record could never be cleared and a stale one
                  # would go on vouching for a value the graph no longer
                  # holds.
                  study._shifted_study_date,
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
    def _reparent_studies(cur, patient, patient_pk) -> None:
        """Point this patient's study rows at it. See `_reparent_series`."""
        uids = [s.study_instance_uid for s in patient.studies]
        if not uids:
            return
        placeholders = ",".join("?" * len(uids))
        cur.execute(
            f"UPDATE studies SET patient_id_fk=? "
            f"WHERE study_instance_uid IN ({placeholders}) AND patient_id_fk!=?",
            (patient_pk, *uids, patient_pk))

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
    def _delete_removed_instances(cur, series, series_pk, held) -> int:
        """Deletes this series' instance rows that no object in the save holds.

        Run for every series, changed or not. Removing an instance from a
        series' list mutates a plain Python list, which marks nothing --
        so the only way to notice a deletion is to compare the two sets.

        The comparison is against `held` -- every UID the saved list
        holds, at any parent -- and not against this series' own list
        (#548): two `Series` objects can share this row, and each list is
        only half of what memory holds. A partial save
        (`prune_absent_patients=False`, a sub-list) still deletes a row
        whose object moved to a patient outside the list, as it always
        did: `held` is built from the list given, not the whole session.
        """
        stored = {row[0] for row in cur.execute(
            "SELECT sop_instance_uid FROM instances WHERE series_id_fk=?",
            (series_pk,)).fetchall()}
        removed = stored - held.instances
        _delete_instances(cur, removed)
        return len(removed)

    @staticmethod
    def _delete_removed_series(cur, study, study_pk, held) -> int:
        """Deletes this study's series that no object in the save holds, and
        their instances. See `_delete_removed_instances` for `held`."""
        stored = {row[1]: row[0] for row in cur.execute(
            "SELECT id, series_instance_uid FROM series WHERE study_id_fk=?",
            (study_pk,)).fetchall()}
        removed = [pk for uid, pk in stored.items() if uid not in held.series]
        _delete_series_subtrees(cur, removed)
        return len(removed)

    @staticmethod
    def _delete_removed_studies(cur, patient, patient_pk, held) -> int:
        """Deletes this patient's studies that no object in the save holds,
        and their subtrees. See `_delete_removed_instances` for `held`."""
        stored = {row[1]: row[0] for row in cur.execute(
            "SELECT id, study_instance_uid FROM studies WHERE patient_id_fk=?",
            (patient_pk,)).fetchall()}
        removed = [pk for uid, pk in stored.items() if uid not in held.studies]
        _delete_study_subtrees(cur, removed)
        return len(removed)

    @staticmethod
    def _delete_absent_patients(cur, patients) -> int:
        """Deletes patient rows the in-memory store no longer contains.

        This is what closes the gap anonymisation opens. Patients are
        upserted on `patient_id`, so changing that value -- exactly what
        de-identification does -- writes a *new* row and orphans the old
        one, with the original name and identifier still in it.
        `_reparent_studies` points every study the patient holds at the
        new row on every save, dirty or not, so nothing ever visits the
        old one again and no scoped deletion reaches it. (Until #551 only
        a study the pass had itself changed was re-parented; a clean one
        stayed under the old row and was deleted here.)

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

        The selection is by object identity, never by SOP Instance UID. A UID
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
        unsaved = [(inst, prepared[inst][0])
                   for inst in series.instances
                   if inst in prepared]
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
        for uid, attributes, attribute_vrs in vertical_rows:
            self.save_vertical_attributes(uid, attributes, conn=conn,
                                          vrs=attribute_vrs)

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
            attributes, private VRs) triples for the vertical table).
        """
        rows, blob_rows, vertical_rows = [], [], []

        for inst, revision in unsaved:
            core, private = _split_core_and_private(self._serialize_item(inst))
            # The VRs for exactly the tags that made it to the private
            # tier, re-keyed to that tier's `("gggg", "eeee")` shape. Only
            # those: an entry for a tag whose value stayed in
            # `attributes_json` would be a VR with no row to sit on
            # (#154).
            private_vrs = {}
            for key in private:
                recorded = inst.attribute_vrs.get(",".join(key))
                if recorded:
                    private_vrs[key] = recorded
            # Appended even when `private` is empty. An instance whose
            # private tags were all stripped still has to reach
            # `save_vertical_attributes`, or its old rows stay in the table
            # and the next reload puts them back on the graph (#158).
            vertical_rows.append((inst.sop_instance_uid, private, private_vrs))

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
            frame = prepared[inst][1]
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
                inst.phi_status.value,
                # NULL keeps a legacy instance legacy for its life in the
                # store; anything this version created or ingested says
                # so (#510).
                None if inst._legacy_shift_provenance else 'recorded'))

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

            # Nested payloads join the same batch (#183). Two things about
            # this loop that a per-blob `persist_blob` would get wrong.
            #
            # It re-emits the **row** every save, keyed on the instance's
            # *current* `sop_instance_uid` -- which is what makes the
            # reference follow a `regenerate_uid()`. `instance_blobs` is
            # keyed by UID, redaction changes it, and a row left under the
            # retired UID is an orphan only `compact()` notices. That went
            # wrong once already for the top-level blob; see
            # `tests/test_redaction_identity.py`.
            #
            # And it never re-appends the **frame**. Nothing in the pipeline
            # mutates an icon: remediation edits `attributes`, redaction
            # touches the top-level array, anonymize touches neither. The
            # bytes were written once at ingest and the ref still points at
            # them.
            #
            # Batched rather than one `persist_blob` per payload, and the
            # gap is not marginal: measured at 200 nested blobs of 4 KiB,
            # `persist_blob` with its own connection each costs 322.8 ms
            # against 11.9 ms for frames appended in the prepass and rows
            # written inside the one transaction. Follow `save_all`'s shape.
            for (path, terminal_tag), ref in inst._nested_pixel_refs.items():
                blob_rows.append((
                    inst.sop_instance_uid,
                    serialize_blob_kind('pixels', path, terminal_tag),
                    ref.offset, ref.length, ref.blob_hash, ref.alg))

        return rows, blob_rows, vertical_rows

    def _prepare_pixel_frames(
            self, patients, tally) -> Dict[Instance, Tuple[int, '_StoredFrame']]:
        """Appends every dirty instance's pixel frame, before any transaction.

        Runs the whole Patient -> Study -> Series -> Instance walk once,
        ahead of `save_all`'s connection, and for each instance with
        unsaved changes captures its revision and calls `_persist_pixels`.
        The transaction that follows therefore does row upserts and
        nothing else -- no compression, no sidecar append, no `flock`
        wait -- so the SQLite write lock is no longer held for the length
        of the save's pixel payload (#287).

        Keyed by **object identity**, which is the only key that
        survives everything that can happen between this walk and the
        transaction's. Position cannot: `series_pk` is unknowable
        before the transaction and a series can be re-parented in
        between -- `_reparent_series` exists precisely because that
        happens. The SOP Instance UID cannot either, and that is the
        sharper trap, because it looks like the natural key: redaction
        mutates `sop_instance_uid` **in place** (`regenerate_uid()`,
        `Session._apply_redaction_outcomes`), so a UID-keyed lookup
        misses a renamed instance -- which then gets skipped by the walk
        while `_delete_removed_instances` deletes its old row as
        orphaned, losing the instance from the index entirely.

        The key is the instance itself. Entities hash and compare by
        identity since #299 (`eq=False` on every `TrackedEntity`
        subclass; `tests/test_entity_state_vocabulary.py` pins it), so
        two field-equal instances are two entries and a renamed one is
        still found. Before #299 `Instance` carried the dataclass default
        `eq=True`, was unhashable, and this had to be `id(inst)`; the
        object key is what #300 closed, and
        `tests/test_save_all_contract.py` asserts the key shape.

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
            Dict[Instance, Tuple[int, _StoredFrame]]: instance ->
            (revision captured before the write, the frame written).
        """
        prepared: Dict[Instance, Tuple[int, '_StoredFrame']] = {}
        for patient in patients:
            for study in patient.studies:
                for series in study.series:
                    for inst in series.instances:
                        if not inst.has_unsaved_changes:
                            continue
                        revision = inst._revision
                        frame = self._persist_pixels(
                            inst, tally, revision=revision)
                        prepared[inst] = (revision, frame)
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
        # No sqlite work happens in here; the caller writes the rows
        # later, off this lock. (This cited a `_pixel_swap_lock` ->
        # `sidecar._lock` order until #366 established that
        # `sidecar._lock` was never acquired by anything. `write_frame`
        # takes an `fcntl.flock` on the file, which is cross-process and
        # a leaf.) The caller -- `save_all`, site 6 -- holds the sidecar
        # gate around the whole prepass and transaction, so the order at
        # this `write_frame` is gate -> `_pixel_swap_lock` -> the leaf
        # flock. Do not take the gate in here: it would be taken under
        # the swap lock, which is the reverse of the order the rewire
        # needs and is what `tests/test_sidecar_gate_order.py` pins
        # (#368).
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
                # diverged array (#293).
                #
                # That does NOT make `arr is None` mean "the array equalled
                # the loader's frame", and it must not be read that way:
                # `discard_pixel_data()`, added in the same change, nulls a
                # diverged array on purpose and leaves the divergence flag
                # set. Nothing else nulls `pixel_array` -- every other
                # write to it (`set_pixel_data()` and `get_pixel_data()`'s
                # three arms) fills it. So `arr is None` here means one of
                # exactly three
                # things: `unload_pixel_data()` cleared an array that was
                # equal to what the loader points at, or a caller
                # deliberately discarded one -- the redaction `finally`
                # blocks, where reverting to the loader's frame IS the
                # intended outcome, and since #434 the discard has also
                # put back the descriptors that frame was stored with, so
                # the row this arm records describes it -- or
                # `Session._apply_redaction_outcomes` nulled it in the
                # same breath as rebinding the loader to the frame the
                # worker just redacted (#322), which is the same intended
                # outcome reached from the parent side of a processes
                # pass. Recording the loader's frame is correct under all
                # three. Do not relax that refusal, or add a fourth
                # nulling site, without revisiting this arm.
                #
                # The loader recorded here may carry a *stale capture*: a
                # descriptor written with the pixels unloaded never passes
                # through a rebuild, so its Rows, BitsAllocated or
                # PixelRepresentation can describe an instance that no
                # longer exists. That is deliberately not repaired here.
                # This arm hands back only the offset, length, algorithm
                # and hash, none of which a descriptor edit changes, and
                # the descriptors reach the store from `attributes`. The
                # staleness is in how the bytes are *read*, and a read
                # before any save was measured identical to one after it,
                # so the check lives on the read: `Instance.get_pixel_data`
                # compares the capture with the instance on every read
                # (`SidecarPixelLoader.describes`) (#417).
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
                # The bytes are the loader's bytes; the DESCRIPTORS may
                # not be. `SidecarPixelLoader.__init__` snapshots rows,
                # columns, samples, frames, BitsAllocated,
                # PixelRepresentation and the `_ISOCENTER_PIXEL_DTYPE`
                # carrier at construction and rebuilds every frame from
                # that snapshot, so a `set_pixel_data()` that changes only
                # the *type* of the pixels -- a `float32` frame handed
                # back as `int32`, an unsigned frame as signed -- leaves
                # the bytes bit-identical, hits this dedup, and is written
                # off by a commit that never re-read the instance. The
                # loader then rebuilds the frame under the superseded
                # dtype, and because the export picks its pixel container
                # from `arr.dtype.kind` (`_export_instance_worker`'s
                # `arr.dtype.kind == 'f'` test) rather than from
                # `attributes`, a float instance whose pixels were
                # replaced with integers exported as `FloatPixelData`
                # beside an audit row reading `wrote 1 of 1` (#406).
                #
                # Rebuilt, not patched: the same window is open on every
                # field the snapshot holds, geometry included -- a
                # replacement of the same byte length at a new
                # Rows/Columns reloads at the old shape, measured. One
                # rebuild answers all eight; a `loader.pixel_dtype = ...`
                # answers one and leaves the rest.
                #
                # This sits *above* the revision guard below, so it reads
                # `inst.attributes` as they are now rather than as they
                # were at the caller's capture. That is pre-existing and
                # harmless: a moved revision leaves the instance dirty
                # against the capture, the next save rebuilds again from
                # the same source, and the offsets handed back are the
                # loader's own, so nothing is poisoned.
                rebuilt = self._create_pixel_loader(
                    loader.offset, loader.length, loader.alg, inst,
                    pixel_hash=digest)
                with entities.PIXEL_STATE_LOCK:
                    inst._pixel_loader = rebuilt
                    # Only if nothing moved since the read. This arm has
                    # no revision guard of its own (it sits above the one
                    # below), so a `set_pixel_data()` landing after the
                    # read had its flag cleared here while its array was
                    # still unwritten (#293's shape), and a discard left
                    # nothing to clear. Checked under the pixel-state
                    # leaf, where both mutators move the revision (#434,
                    # Q6).
                    if (inst.pixel_array is arr
                            and (revision is None or inst._revision == revision)):
                        # These exact bytes are already in the sidecar
                        # and the loader already points at them, so the
                        # resident array is recoverable and freeable
                        # again (#293).
                        inst._pixel_array_unwritten = False
                        # The loader just rebuilt describes them under
                        # the current descriptors, so a discard has
                        # nothing to put back (#434) -- a same-bytes,
                        # new-dtype replacement is saved here, not
                        # appended below.
                        inst._pixel_descriptors_replaced = None
                return _StoredFrame(loader.offset, loader.length,
                                    loader.alg, digest)

            offset, length = self.sidecar.write_frame(raw, _PIXEL_COMPRESSION)
            tally.pixel_bytes += length
            tally.pixel_frames += 1

            # Re-point the loader so the array can be unloaded safely
            # later. `pixel_hash=digest` is passed explicitly, the way
            # `persist_pixel_data` does: left to default, the loader falls
            # back to `inst._pixel_hash`, which at this point is still the
            # digest of the frame these bytes just replaced -- so the next
            # read after an unload raised an integrity mismatch against
            # correctly-saved data (#212). Passing it removes the ordering
            # dependency between this call and the assignment below.
            written = self._create_pixel_loader(
                offset, length, _PIXEL_COMPRESSION, inst, pixel_hash=digest)
            # The guard and the publish under the pixel-state leaf, and
            # the guard inside it: outside, a `discard_pixel_data()`
            # landing after the check and before the clears restored the
            # pre-set descriptors over the frame being published, and a
            # `set_pixel_data()` there had its flag cleared by a save of
            # the previous array (#434, Q6). Both mutators move the
            # revision under the same lock, so the guard sees them. The
            # loader is built before it: construction reads attributes
            # and is wasted only when the guard skips.
            with entities.PIXEL_STATE_LOCK:
                if revision is not None and inst._revision != revision:
                    # The bytes read above no longer describe the instance:
                    # a mutation (a redaction, most importantly) landed after
                    # the caller's capture. Publishing them would write a row
                    # and a loader for state the graph has already left --
                    # exactly #274's poisoning. The frame already appended is
                    # a harmless orphan; the instance is still dirty against
                    # the captured revision, so the next save corrects the row.
                    return _StoredFrame(None, None, None, None)

                inst._pixel_loader = written
                inst._pixel_hash = digest
                # Published: the loader now points at these bytes, so the
                # resident array is recoverable and freeable. This clear is
                # what keeps `release_memory()` working after a
                # `set_pixel_data()`; miss it and every replaced-and-saved
                # instance becomes permanently unfreeable, silently, because
                # the sweep only logs counts (#293).
                inst._pixel_array_unwritten = False
                # Written, so it is the stored frame and a discard has
                # nothing to undo (#434). After the revision guard above,
                # never before it: a skipped publish leaves the replacement
                # unwritten, and its record must survive for a discard.
                inst._pixel_descriptors_replaced = None
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
            self.logger.error(f"Failed to count instances: {describe_exception(e)}")
            return 0

    def get_flattened_instances(self,
                                patient_ids: List[str] = None,
                                instance_uids: List[str] = None,
                                page_size: int = _FLATTENED_PAGE_SIZE):
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
            patient_ids (List[str], optional): Restrict the rows to these
                Patient IDs. ``None`` means every patient in the store.
                An empty list matches nobody -- it is a filter that
                selected nothing, not an absent filter.
            instance_uids (List[str], optional): Restrict the rows to
                these SOP Instance UIDs. Same rule: ``None`` is no
                filter, an empty list matches nobody. Both filters
                together intersect.
            page_size (int, optional): Rows per page, defaulting to 500.
                Trades resident memory against the number of queries.
                Must be an `int` >= 1 -- `LIMIT 0` returns an empty page,
                and an empty page is how the walk decides it has
                finished, so a zero would silently report an empty store.

        Yields:
            dict: Flattend dictionary representing row data (patient, study, series, instance paths).

        Raises:
            ValueError: If `page_size` is not a whole number >= 1. This
                is a plain method wrapping a generator precisely so the
                check fires at the call, not on the first `next()` --
                which is what a `2.5` used to do, reaching `LIMIT ?` and
                raising `sqlite3.IntegrityError: datatype mismatch` a
                page later, out of a public method, for a caller's typo.
        """
        # `bool` before `int`, and the ordering is the mechanism:
        # `isinstance(True, int)` is True and `True < 1` is False, so
        # `page_size=True` passed the old guard and would pass a bare
        # `isinstance(page_size, int)` too -- and then page one row at a
        # time, silently. Same subclass trap, and the same answer, as
        # `_fallback_encoding`'s `bool` arm and `_value_fits_vr`'s
        # ordering in `io_handlers.py` (#283).
        #
        # No `int()` coercion for a float. `1e9` is a whole number and
        # `2.5` is not, and a rule that accepted one would have to
        # decide what to do with the other; refusing the type outright
        # says which spelling this method takes.
        if (isinstance(page_size, bool)
                or not isinstance(page_size, int)
                or page_size < 1):
            raise ValueError(
                f"page_size must be a whole number >= 1, got {page_size!r}")
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

        # `is not None` rather than a truth test: `[]` must exclude
        # everyone (#142). A caller computing a cohort that came back
        # empty would otherwise walk the whole store -- silent
        # over-export, from the DB reader the changelog points migrating
        # `export_to_parquet` callers at. Same rule, same comment, as
        # `get_cohort_report` and `_export_dicom` in `session.py`; the
        # truth test here was the one reader of three that got it wrong.
        #
        # The empty list renders `p.patient_id IN ()`, and no
        # short-circuit is added for it because SQLite accepts it as
        # legal and false. That is a dialect extension, not SQL:
        # sqlite.org/lang_expr.html -- "SQLite allows the parenthesized
        # list of scalar values on the right-hand side of an IN or NOT
        # IN operator to be an empty list but most other SQL database
        # engines and the SQL92 standard require the list to contain at
        # least one element." A port to another engine needs a
        # short-circuit here; on SQLite one would be a second mechanism
        # for one rule.
        if patient_ids is not None:
            placeholders = ",".join("?" for _ in patient_ids)
            filters.append(f"p.patient_id IN ({placeholders})")
            filter_params.extend(patient_ids)

        if instance_uids is not None:
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
            self.logger.error(f"Failed to update attributes: {describe_exception(e)}")

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
            self.logger.error(f"Failed to save findings: {describe_exception(e)}")

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
            self.logger.error(f"Failed to load findings: {describe_exception(e)}")

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

            # No `self.sidecar = SidecarManager(...)` rebind here. It was
            # inert -- `SidecarManager` holds only `filepath`, and
            # `write_frame`/`read_frame` open by path on every call, so the
            # replacement was indistinguishable from the object it replaced
            # (#366). Deleted rather than kept "for safety": a rebind that
            # does nothing reads as though it were re-pointing a writer at
            # the compacted file, which is a guarantee this code does not
            # make and cannot make -- a concurrent writer holds whatever
            # manager it already read. What does close that is the sidecar
            # gate (#368), which `Session.compact()` holds across this
            # method AND the loader rewire after it; this method takes no
            # lock of its own, deliberately, so the hold and the rewire
            # cannot be split. A direct caller of this method gets the
            # predicate and nothing else --
            # `tests/test_compaction_reclaims_a_row_instances_does_not_carry.py`
            # is the record of what that means.
            self._log_compaction_result(start_time, original_size, written_bytes)
            return uid_map

        except Exception as exc:
            self.logger.error(f"Compaction Failed: {describe_exception(exc)}")
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
            self.logger.error(f"Compaction Failed (Query): {describe_exception(exc)}")
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
                    "Could not remove temporary sidecar %s: %s", temp_path, describe_exception(exc))

        # Only ever discard the backup once the real sidecar is back in
        # place -- otherwise it is the last copy of the data.
        if os.path.exists(backup_path) and os.path.exists(self.sidecar_path):
            try:
                os.remove(backup_path)
            except OSError as exc:
                self.logger.warning(
                    "Could not remove stale sidecar backup %s: %s",
                    backup_path, describe_exception(exc))

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

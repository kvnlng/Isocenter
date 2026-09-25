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

from pydicom import config as pydicom_config
from pydicom.multival import MultiValue
from pydicom.valuerep import DSdecimal, DSfloat, IS, ISfloat

from .entities import (Patient, Study, Series, Instance, Equipment,
                       PhiStatus, ScanPolicy, normalize_study_date,
                       resolve_item_path)
from . import entities
from .blob_kind import parse_blob_kind, serialize_blob_kind
from .sidecar import SidecarManager
from .logger import describe_exception, get_logger
from .privacy import (PhiFinding, PhiRemediation, _is_keyed_pseudonym_shape,
                      _is_replacement_id, _is_unkeyed_pseudonym_shape,
                      _has_minted_uid_shape, _pseudonym_verifies, _unkeyed_replacement_id_for)
from .io_handlers import (NestedPixelRef, SidecarPixelLoader,
                          nested_item_geometry, normalize_id_filter)



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
                           phi_status, shift_provenance, phi_policy, phi_policy_base,
                           phi_status_edited)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        -- The policy the status was recorded under (#555), plainly for
        -- the same reason: a status that went UNSCANNED writes NULL here,
        -- and a guard would keep the old policy beside it.
        phi_policy=excluded.phi_policy,
        phi_policy_base=excluded.phi_policy_base,
        -- The status an edit left stale (#767), plainly: a status a scan
        -- has since read again writes NULL here, and a guard would keep
        -- the stale claim beside the fresh one.
        phi_status_edited=excluded.phi_status_edited,
        pixel_offset=COALESCE(excluded.pixel_offset, instances.pixel_offset),
        pixel_length=COALESCE(excluded.pixel_length, instances.pixel_length),
        pixel_hash=COALESCE(excluded.pixel_hash, instances.pixel_hash),
        compress_alg=COALESCE(excluded.compress_alg, instances.compress_alg)
"""


def _phi_status_from_stored(value) -> PhiStatus:
    """The status a stored row claims, defaulting to UNSCANNED.

    Args:
        value: The stored `phi_status` text, or None.

    Returns:
        PhiStatus: The status; UNSCANNED for NULL and for any value not
            recognised, so an unrecognised claim never presents as an
            assurance.
    """
    try:
        return PhiStatus(value)
    except ValueError:
        return PhiStatus.UNSCANNED


def _stale_status_from_stored(value) -> Optional[PhiStatus]:
    """The status a row's `phi_status_edited` says an edit left stale.

    Args:
        value: The stored `phi_status_edited` text, or None.

    Returns:
        Optional[PhiStatus]: The stale status, or None for NULL (not
            stale), UNSCANNED, or a value not recognised.
    """
    try:
        status = PhiStatus(value)
    except ValueError:
        return None
    return None if status is PhiStatus.UNSCANNED else status


def _status_columns(entity):
    """The four status columns for an entity's row.

    An UNSCANNED status, and a status recorded without a policy, write
    NULL for both policy columns. A stale status -- recorded, then left
    behind by an edit no scan has read -- writes `'unscanned'` with the
    stale status in `phi_status_edited`.

    Args:
        entity: A Patient, Study or Instance.

    Returns:
        tuple: `(phi_status, phi_policy, phi_policy_base,
            phi_status_edited)`.
    """
    # One read of the record, in `_phi_status_record`'s order (the recorded
    # revision first, the current one last), so a save racing an audit
    # writes a status with its own policy or neither changed, never one
    # status beside another's policy.
    #
    # A stale status keeps `phi_status` UNSCANNED: a build that does not
    # read `phi_status_edited` must read the entity as unscanned, never the
    # stale status as a current one, which would grade PASS and let the
    # export write `(0012,0062) YES` over a value no scan read. The column
    # lets hydration restore "scanned, then edited" (grade condition 8).
    recorded_at = entity._phi_status_revision
    status = entity._phi_status
    policy = entity._phi_status_policy
    current = entity._revision
    if status is None or status is PhiStatus.UNSCANNED:
        return PhiStatus.UNSCANNED.value, None, None, None
    if recorded_at != current:
        return PhiStatus.UNSCANNED.value, None, None, status.value
    if policy is None:
        return status.value, None, None, None
    return status.value, policy.fingerprint, policy.base, None


def _in_clause(values):
    """A parameter placeholder list for an IN clause of this length.

    Args:
        values: The values the clause will bind.

    Returns:
        str: `"?,?,..."`, one `?` per value.
    """
    return ",".join("?" * len(values))


class _HeldUids(NamedTuple):
    """Every study, series and instance UID a list of patients holds."""
    studies: Set[str]
    series: Set[str]
    instances: Set[str]


def _held_uids(patients) -> _HeldUids:
    """What a save must not delete: each UID any object in `patients` holds.

    Call when the scoped deletes run, so a UID renamed in place since the
    prepass is read as it is now.

    Args:
        patients: The patients being saved.

    Returns:
        _HeldUids: The study, series and instance UIDs they hold.
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
    """One WARNING when two `Patient` objects in a save carry one ID.

    The store holds one `patients` row per ID, so the row's name and
    status come from the last of those objects the save writes: each one
    with unsaved changes, and the first one walked if the store had no
    row for the ID (`_upsert_patient`). If none is written the row keeps
    what it held. Every study is kept (`_held_uids`). The line carries a
    count only, never a Patient ID.

    Args:
        logger: Where to log.
        patients: The patients being saved.
    """
    # Counts only: a log shipped beside an export must not pair identities
    # with what replaced them.
    per_id = Counter(p.patient_id for p in patients)
    sharing = sum(n for n in per_id.values() if n > 1)
    if sharing:
        logger.warning(
            "%d Patient objects share a Patient ID with another; the store "
            "holds one row for each ID and keeps every study; reload to see "
            "them as one patient", sharing)


def _delete_instances(cur, uids) -> None:
    """Deletes instances and their attribute and blob rows.

    Args:
        cur: A cursor in the caller's transaction.
        uids: SOP Instance UIDs to delete; empty does nothing.
    """
    # Explicitly, table by table: `instance_attributes` declares
    # `ON DELETE CASCADE`, but SQLite enforces foreign keys only under
    # `PRAGMA foreign_keys=ON`, which this store never sets. Leaving those
    # rows behind would leave private tag *values* in the database, still
    # attributable by UID.
    if not uids:
        return
    rows = [(uid,) for uid in uids]
    cur.executemany("DELETE FROM instance_attributes WHERE instance_uid=?", rows)
    cur.executemany("DELETE FROM instance_blobs WHERE instance_uid=?", rows)
    cur.executemany("DELETE FROM instances WHERE sop_instance_uid=?", rows)


def _delete_series_subtrees(cur, series_pks) -> None:
    """Deletes series rows and every instance beneath them.

    Args:
        cur: A cursor in the caller's transaction.
        series_pks: Series primary keys; empty does nothing.
    """
    if not series_pks:
        return
    clause = _in_clause(series_pks)
    uids = [row[0] for row in cur.execute(
        f"SELECT sop_instance_uid FROM instances WHERE series_id_fk IN ({clause})",
        series_pks).fetchall()]
    _delete_instances(cur, uids)
    cur.execute(f"DELETE FROM series WHERE id IN ({clause})", series_pks)


def _delete_study_subtrees(cur, study_pks) -> None:
    """Deletes study rows and every series and instance beneath them.

    Args:
        cur: A cursor in the caller's transaction.
        study_pks: Study primary keys; empty does nothing.
    """
    if not study_pks:
        return
    clause = _in_clause(study_pks)
    series_pks = [row[0] for row in cur.execute(
        f"SELECT id FROM series WHERE study_id_fk IN ({clause})",
        study_pks).fetchall()]
    _delete_series_subtrees(cur, series_pks)
    cur.execute(f"DELETE FROM studies WHERE id IN ({clause})", study_pks)


def _delete_patient_subtrees(cur, patient_pks) -> None:
    """Deletes patient rows and everything beneath them.

    Args:
        cur: A cursor in the caller's transaction.
        patient_pks: Patient primary keys; empty does nothing.
    """
    if not patient_pks:
        return
    clause = _in_clause(patient_pks)
    study_pks = [row[0] for row in cur.execute(
        f"SELECT id FROM studies WHERE patient_id_fk IN ({clause})",
        patient_pks).fetchall()]
    _delete_study_subtrees(cur, study_pks)
    cur.execute(f"DELETE FROM patients WHERE id IN ({clause})", patient_pks)


#: SQLite busy timeout for every file-backed connection, in seconds.
#: A lock that will not clear must surface as `sqlite3.OperationalError:
#: database is locked` *inside* one faulthandler window (pytest's
#: `faulthandler_timeout=300`), where the dump shows a thread still
#: waiting with a stack -- never as a stall for an outer timeout to kill.
#: Keep it under that window. The transaction it bounds holds row upserts
#: only (sidecar writes run in a prepass), so its length is bounded by row
#: count, not pixel bytes; that is not a reason to shrink the number. The
#: timeout is a diagnostic for a STUCK writer (another process, a stale
#: WAL). Keep it a named constant that `_get_connection` reads: an inline
#: literal escapes the check that it stays under the window. No
#: environment variable on purpose (one spelling per behaviour); tests
#: monkeypatch the constant.
_SQLITE_BUSY_TIMEOUT_S = 120.0

#: How long a sidecar writer, a redact()/ingest() pass, or `compact()`
#: waits for the sidecar gate or the pass-lock before raising.
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
#: cap, ends the test. Keep it a named constant that `_hold_sidecar_gate`
#: reads. No environment variable on purpose (one spelling per
#: behaviour); tests monkeypatch the constant. The value tolerates a
#: compaction of ~800 MB live on 100 MB/s storage behind a stuck sqlite
#: writer.
_SIDECAR_GATE_TIMEOUT_S = 180.0

#: The poll interval for the two bounded flock loops. `flock` has no
#: timeout and `signal.alarm` does not reach non-main threads, so the
#: bound is a `LOCK_NB` attempt every 10 ms. The uncontended path takes
#: the lock on the first attempt; a contended one pays at most one
#: interval on top of the hold it waited for.
_LOCK_POLL_INTERVAL_S = 0.01


@contextlib.contextmanager
def _flock_within(path, flags, deadline, describe):
    """Hold `flags` on `path` for the block, polling `LOCK_NB` until `deadline`.

    Creates `path` if it does not exist. The lock is released and the file
    descriptor closed on every exit.

    Args:
        path (str): The lock file.
        flags (int): `fcntl.LOCK_SH` or `fcntl.LOCK_EX`.
        deadline (float): A `time.monotonic()` value.
        describe: Called only on expiry; returns the error message.

    Yields:
        int: The locked file descriptor, while the lock is held.

    Raises:
        RuntimeError: The lock was not acquired by `deadline`.
    """
    # One fd per acquisition, closed on every exit including exception: a
    # leaked fd holds the flock for the life of the process, and `flock` is
    # per open file description, so a second fd on the same path from the
    # same thread would deadlock against the first.
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


class _NoRowFor(Exception):
    """`update_attributes` matched fewer rows than it was given.

    Never leaves `update_attributes`, which turns it into a
    `RuntimeError`.

    Args:
        shortfall (int): How many instances matched no row.
        total (int): How many instances the write was given.
    """
    # Raised inside the connection's `with` so the write rolls back. Not a
    # `sqlite3.Error`, so that handler cannot catch it, and not a
    # `RuntimeError`, so nothing between the raise and its own `except` can
    # mistake it for the public one.

    def __init__(self, shortfall: int, total: int):
        super().__init__(shortfall, total)
        self.shortfall = shortfall
        self.total = total


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

    Args:
        value: A `date`, a string, or None.

    Returns:
        Optional[str]: ISO text for a date, `str(value)` otherwise, None
            for None.
    """
    # Python 3.12 deprecated sqlite3's default date adapter, so dates are
    # converted here rather than left for sqlite3 to guess at.
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _as_loaded_date(value):
    """The inverse of `_as_stored_date`: text back into a `date`.

    Gives `Study.study_date` the `datetime.date` type ingest produces.
    Both `YYYY-MM-DD` and `YYYYMMDD` become a `date`; an unparseable value
    is returned as stored, and None stays None.

    Args:
        value: The stored `study_date` text, or None.

    Returns:
        A `date`, or `value` unchanged when it cannot be parsed.
    """
    # One type everywhere: both exporters read the field without checking
    # its type, and a stored ISO string would export as `'2024-01-15'`,
    # not a DA value. Do not "tighten" this to a strict `%Y-%m-%d` parse,
    # which would leave a `YYYYMMDD` string a second type. Keep the body
    # `entities.normalize_study_date`, which `Study.__setattr__` also
    # applies: two copies of the rule can disagree, and the export
    # directory name (`export_folder_names`, which formats with `str()`)
    # would then differ before and after a round trip.
    return normalize_study_date(value)


#: How to read a `value_text` back for the VRs whose values are *not*
#: text on the wire. The vertical table's column is TEXT, so every value
#: is stored stringified; for these VRs pydicom refuses the string at
#: write time -- `US`/`UL`/`FL` raise `struct.error: required argument
#: is not an integer` from `filewriter.write_numbers`, which fails the
#: whole export rather than the element. Restoring the type is therefore
#: part of restoring the VR, not a separate nicety.
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

    `_vertical_atom_value` is its inverse; change the two together.

    Args:
        vr (str): The element's VR.
        atom: One value.

    Returns:
        Optional[str]: None for None (stored as SQL NULL); the decimal
            integer for an `AT` value; `str(atom)` otherwise.
    """
    # `AT`'s `str()` is the display spelling `'(0010,0010)'`, which `Tag()`
    # refuses to read back, so it is stored as the decimal integer it is.
    #
    # `None` is not stringified: `str(None)` is the text `None`, a
    # conformant `LO` value, so a zero-length private element would reload
    # as a word the source never said. This guard has to come BEFORE the
    # `AT` arm: there `int(None)` raises `TypeError`, the arm's `except`
    # catches it, and the same fabricated `'None'` comes back.
    if atom is None:
        return None
    if vr == 'AT':
        try:
            return str(int(atom))
        except (TypeError, ValueError):
            # A value that is no longer a tag. Stored as it reads and
            # reloaded as text; `_value_fits_vr` refuses it at export
            # and the fallback runs.
            return str(atom)
    return str(atom)


def _vertical_atom_value(vr: str, text: Optional[str]) -> Any:
    """The inverse of `_vertical_atom_text`, keyed on the stored VR.

    Args:
        vr (str): The stored VR.
        text (Optional[str]): The stored `value_text`.

    Returns:
        None for NULL; an `int` or `float` for the VRs in
            `_VERTICAL_VR_PARSERS` when the text parses; the text otherwise.
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
        # fallback -- never worse than no recorded VR at all.
        return text


def _is_private_tag(key) -> bool:
    """Whether `key` is a well-formed "gggg,eeee" tag in an odd group.

    Args:
        key: An attribute key.

    Returns:
        bool: True for a private tag; False for anything else, including
            a key that is not a tag.
    """
    # The one statement of the storage split: `_split_core_and_private`
    # routes by it and `_serialize_item` picks the root `__vrs__` by it, so
    # the two cannot disagree about which tags the vertical table holds.
    try:
        return int(key.split(',')[0], 16) % 2 != 0
    except (ValueError, AttributeError):
        return False


def _split_core_and_private(attributes: Dict[str, Any]) -> Tuple[Dict[str, Any],
                                                                 Dict[Tuple[str, str], Any]]:
    """Separates private tags from the ones stored inline as JSON.

    Odd DICOM groups are private (PS3.5 §7.8) and go to the vertical
    `instance_attributes` table, where they can be queried per tag rather
    than by parsing every instance's JSON blob.

    Two things stay inline regardless: `__sequences__`, which is nested
    structure the vertical table has no shape for, and any `bytes` value,
    which that table's TEXT column cannot hold.

    Args:
        attributes (Dict[str, Any]): A serialized instance.

    Returns:
        Tuple of (core attributes keyed by "gggg,eeee", private attributes
            keyed by a ("gggg", "eeee") tuple).
    """
    core, private = {}, {}

    for key, value in attributes.items():
        if key == "__sequences__":
            core[key] = value
            continue

        # A key that is not a well-formed "gggg,eeee" pair is kept as a
        # standard attribute rather than guessed at (`_is_private_tag`).
        if _is_private_tag(key) and not isinstance(value, bytes):
            private[tuple(key.split(','))] = value
        else:
            core[key] = value

    return core, private



def _report_abandoned_audit_rows(audit_queue):
    """Log a warning that a collected store took undrained audit rows with it.

    Args:
        audit_queue (queue.Queue): The collected store's queue.
    """
    # The worker holds its store weakly, so a store with queued rows can be
    # collected, and holds the queue strongly so this can count what is
    # dropped rather than drop it silently.
    pending = audit_queue.qsize()
    if pending:
        get_logger().warning(
            f"An SqliteStore was collected with {pending} audit row(s) "
            f"still queued; those rows are lost. Call stop() -- or close "
            f"the session -- to settle the audit log before dropping a "
            f"store (#316).")


def _audit_worker_loop(store_ref, stop_event, wakeup, audit_queue):
    """Background audit writer that does not keep its store alive.

    Drains the store's queue at least once a second and whenever woken.
    Exits on `stop()` after a final drain, or when the store has been
    collected, logging any rows it could not write.

    Args:
        store_ref (weakref.ref): The store, held weakly.
        stop_event (threading.Event): Set by `stop()`.
        wakeup (threading.Event): Set by `log_audit`.
        audit_queue (queue.Queue): The store's queue.
    """
    # Module-level, taking a **weak** reference: a bound method as the
    # thread target would hold `self`, and a running `Thread` holds its
    # target, so the store would stay alive while the worker ran.
    #
    # Exiting on a dead weakref is safe: `flush_audit_queue()` drains on
    # the *caller's* thread under `_audit_write_lock`, so the read barrier
    # does not depend on this worker at all; the worker only removes
    # background latency.
    #
    # Two things not to "simplify": `del store` before returning to the
    # wait (a strong reference held across the one-second wait keeps the
    # store alive, a second at a time, forever); and the Events and the
    # Queue are held strongly, which is safe because `threading.Event`
    # references nothing and audit rows are plain string tuples.
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
    """The session's SQLite store and pixel sidecar: `session.store_backend`.

    It holds the Patient -> Study -> Series -> Instance hierarchy, the
    append-only `<name>_pixels.bin` sidecar, and the audit log, which a
    background thread writes.
    """
    # Picklable: a clone gets fresh locks and its own audit worker, and
    # never deletes the parent's temporary sidecar. Call `stop()` to settle
    # the audit log before dropping a store.

    #: Rows `get_flattened_instances` fetches per page.
    #:
    #: Trades resident memory against query count. A page is dominated by
    #: `attributes_json` -- every standard attribute of the instance, as
    #: text -- not by the sixteen scalar columns beside it, so the number
    #: that matters is roughly `page_size x blob size`. At 500 that is
    #: single-digit megabytes for ordinary CT metadata, which keeps the
    #: method's memory promise on 100GB+ datasets, while the per-page cost
    #: (one rowid seek plus `page_size` primary-key joins) disappears into
    #: the noise.
    #:
    #: The *default*, not a knob: `get_flattened_instances` takes it as a
    #: default argument, which Python evaluates once when the `def` runs,
    #: so rebinding this attribute -- on the class or on a subclass --
    #: changes nothing. `page_size=` is the one spelling for the behaviour.
    _FLATTENED_PAGE_SIZE = 500

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS patients (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT NOT NULL,
        patient_name TEXT,
        phi_status TEXT,
        phi_policy TEXT,      -- #555: 'v1:' + sha256 hex; NULL = none known
        phi_policy_base TEXT, -- #555: the policy's readable base
        phi_status_edited TEXT, -- #767: the status an edit left stale; NULL = none
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
        phi_policy TEXT,      -- #555: 'v1:' + sha256 hex; NULL = none known
        phi_policy_base TEXT, -- #555: the policy's readable base
        phi_status_edited TEXT, -- #767: the status an edit left stale; NULL = none
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
        -- The policy `phi_status` was recorded under (#555): 'v1:' plus
        -- the sha256 of `configuration._canonical_policy_v1`, and its
        -- readable base. NULL: an unscanned status, or a row written
        -- before 1.0, whose policy nothing recorded.
        phi_policy TEXT,
        phi_policy_base TEXT,
        -- The status an edit left stale (#767): `phi_status` says
        -- 'unscanned' beside it, as a build before this column reads it.
        phi_status_edited TEXT,
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
        """Open (or create) the store and start its audit worker.

        Creates the schema and adds any missing columns. A file store's
        sidecar is `<db_path without extension>_pixels.bin`; a `:memory:`
        store uses a temporary sidecar file that `stop()` deletes.

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
            # drops the flag and `__setstate__` sets it False.
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
        # its first tick.
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
        # stale hash matches the stale frame. Never held across a
        # sqlite write, whose busy timeout can be waited out while
        # holding it.
        #
        # Frame writers serialise with each other on `fcntl.flock` inside
        # `write_frame`, which is cross-process, and `read_frame` does not
        # take it at all. Writers against `compact_sidecar` are serialised
        # by the gate below, which sits ABOVE this lock.
        self._pixel_swap_lock = threading.Lock()
        # The sidecar gate's in-process half. Mutual exclusion
        # between every frame writer and the compaction rewrite; the
        # cross-process half is `fcntl.flock` on `_gate_path()`, taken
        # inside `_hold_sidecar_gate` only after this lock is held, so
        # two threads of one process never hold two fds on the lock
        # file at once. Lock order, an internal invariant:
        #
        #     _sidecar_gate -> _pixel_swap_lock          (rewire; sites 5, 6)
        #     _sidecar_gate -> sqlite                    (every site's commit)
        #     _sidecar_gate -> pass-lock, LOCK_NB only   (compact's refusal)
        #     _pixel_swap_lock -> entities.PIXEL_STATE_LOCK  (publish; leaf)
        #
        # `PIXEL_STATE_LOCK` is innermost: taken by the pixel mutators
        # holding nothing and by the publish sections under this lock, and
        # never held while taking any other lock, a flock or sqlite.
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
        """Exclude threading primitives and connections from pickling.

        Returns:
            dict: The instance state without them.
        """
        state = self.__dict__.copy()
        keys_to_remove = [
            '_memory_lock',
            '_memory_conn',
            'audit_queue',
            '_stop_event',
            # A lock and an Event both raise `TypeError: cannot pickle
            # '_thread.lock' object`. Adding an audit primitive without
            # adding it here breaks *every* pickle of a store.
            '_audit_write_lock',
            '_audit_wakeup',
            '_audit_drop_lock',
            '_pixel_swap_lock',
            # The gate's thread lock. The clone recreates its own and
            # opens its own fd on the same lock path per acquisition;
            # the flock, not this lock, is what reaches across.
            '_sidecar_gate',
            '_audit_thread',
            # Not a threading primitive, but dropped for the same
            # reason a clone gets fresh locks: a clone that inherited
            # `True` would unlink the parent's sidecar on its own
            # `stop()`. `__setstate__` sets it False.
            '_owns_temp_sidecar']
        for k in keys_to_remove:
            state.pop(k, None)
        return state

    def __setstate__(self, state):
        """Recreate threading primitives and start a new audit worker.

        A clone never owns the temporary sidecar. On a `:memory:` store the
        clone has no connection.

        Args:
            state (dict): The pickled state.
        """
        self.__dict__.update(state)

        # Restore non-pickleable attributes
        if self.db_path == ":memory:":
            self._memory_lock = threading.Lock()
            self._memory_conn = None  # Connection lost on pickle transfer
        else:
            self._memory_lock = None
            self._memory_conn = None
        # A clone never owns the temp sidecar, whatever the parent did:
        # a clone's `stop()` can run while the parent is still reading.
        self._owns_temp_sidecar = False

        self.audit_queue = queue.Queue()
        self._stop_event = threading.Event()
        # See `__init__`: before the worker starts, not after.
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

        Returns:
            str: The path, beside the sidecar; never replaced.
        """
        # Never the sidecar itself: `flock` binds to an inode, and
        # `compact_sidecar` swaps the sidecar in with `os.replace`, which
        # gives the path a new inode, so a writer blocked on the old one
        # would wake after the swap and append into the unlinked file.
        return self.sidecar_path + ".lock"

    def _pass_lock_path(self) -> str:
        """The pass-lock's file: `<sidecar>.pass.lock`.

        Returns:
            str: The path.
        """
        return self.sidecar_path + ".pass.lock"

    def _gate_timeout_message(self) -> str:
        """The error text for a sidecar gate timeout.

        Returns:
            str: The message every channel carries, naming the lock file
                and `_SIDECAR_GATE_TIMEOUT_S`.
        """
        return (f"Sidecar gate {self._gate_path()} not acquired within "
                f"_SIDECAR_GATE_TIMEOUT_S={_SIDECAR_GATE_TIMEOUT_S:g} s; a "
                "compaction or another writer is holding it")

    @contextlib.contextmanager
    def _hold_sidecar_gate(self):
        """Hold the sidecar gate for the block.

        Mutual exclusion, across threads and processes, between every frame
        writer and the compaction rewrite. Hold it across a frame append
        **and** the commit of the row that names it; `Session.compact()`
        holds it across `compact_sidecar()` **and**
        `_rewire_sidecar_loaders()`. Never take it while holding
        `_pixel_swap_lock`.

        A `close()` whose persistence worker is queued behind a compaction
        longer than `_SHUTDOWN_JOIN_TIMEOUT_S` (30 s -- about 3 GB live on
        local SSD, ~300 MB on 100 MB/s network storage) treats the worker
        as wedged although the compaction is healthy.

        Yields:
            None: While the gate is held.

        Raises:
            RuntimeError: The gate was not acquired within
                `_SIDECAR_GATE_TIMEOUT_S` (one budget for both halves); the
                message names the lock file and the constant.
        """
        # Thread lock first, then an exclusive `flock` on `_gate_path()`,
        # the stable path beside the sidecar: a flock binds to an inode and
        # compaction's `os.replace` gives the sidecar a new one. The thread
        # lock keeps two threads of one process from holding two fds on the
        # lock file at once. Where an expiry lands: a redaction worker
        # returns it as `RedactionOutcome(ok=False)` and the parent raises
        # `RedactionError` with an ERROR audit row; ingest files an ERROR
        # audit row per result; a background save logs `Background save
        # failed` and leaves its instances dirty (no audit row);
        # `save(sync=True)` and `compact()` raise to the caller.
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
        """Hold the pass-lock shared for a `redact()`/`ingest()` pass.

        Take it holding no other lock, for the whole pass: from before the
        first worker can call `regenerate_uid()` until after
        `_apply_redaction_outcomes` has bound every loader. While any pass
        holds it, `compact()` refuses. A pass that starts while a
        compaction is running waits behind it (its leading save and its
        rewrite), bounded by `_SIDECAR_GATE_TIMEOUT_S`, then proceeds.
        Released by the kernel if the process dies.

        Yields:
            None: While the lock is held.

        Raises:
            RuntimeError: A running compaction held the lock for longer than
                `_SIDECAR_GATE_TIMEOUT_S`.
        """
        # During a pass the graph carries references the store has not been
        # told about yet -- a worker commits its blob row under a
        # regenerated UID before any `instances` row names it -- and
        # compaction's orphan predicate is correct to reclaim such a row.
        # So compaction is kept out until the pass has told the store; do
        # not teach the predicate a second answer to "what is live" instead.
        deadline = time.monotonic() + _SIDECAR_GATE_TIMEOUT_S
        path = self._pass_lock_path()

        def describe():
            """The timeout message.

            Returns:
                str: The message, naming the lock file and the constant.
            """
            return (f"Pass-lock {path} not acquired within "
                    f"_SIDECAR_GATE_TIMEOUT_S={_SIDECAR_GATE_TIMEOUT_S:g} s; "
                    "a compaction is saving or rewriting the sidecar")

        with _flock_within(path, fcntl.LOCK_SH, deadline, describe):
            yield

    @contextlib.contextmanager
    def _refuse_while_pass_open(self):
        """Hold the pass-lock exclusive for a compaction, or refuse.

        Take it holding no other lock, before `compact()`'s leading save,
        and hold it through the rewrite and the rewire: a pass starting
        anywhere inside waits. Does not wait for an open pass.

        Yields:
            None: While the lock is held.

        Raises:
            RuntimeError: A `redact()` or `ingest()` pass is open; nothing
                has happened yet.
        """
        # Before the leading save, not under the gate: otherwise a pass
        # that opens and closes inside that save is admitted with its rows
        # unnamed, and the rewrite reclaims its frames. `LOCK_NB` because
        # the refusal is an answer, not a wait (a pass holds SH for its
        # whole length, and waiting for it is the caller's call to make).
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
        """A connection for one transaction, committed on exit.

        A file store opens a fresh connection (busy timeout
        `_SQLITE_BUSY_TIMEOUT_S`) and closes it on exit. A `:memory:` store
        reuses its one connection under a non-reentrant lock, so never
        nest two of these on one store. Any exception rolls back and is
        re-raised.

        Yields:
            sqlite3.Connection: The connection, with `sqlite3.Row` rows.
        """
        if self._memory_conn:
            # For in-memory DB, reuse the single connection.
            # We must serialize access because sqlite3 connections are not thread-safe
            # for concurrent writes even with check_same_thread=False.
            with self._memory_lock:
                try:
                    yield self._memory_conn
                    self._memory_conn.commit()
                except Exception as e:
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

        Idempotent. Existing rows read a new column as NULL; nothing is
        back-filled. Ends by classifying every patient row whose jitter
        scheme is NULL.

        Args:
            conn (sqlite3.Connection): An open connection in the caller's
                transaction.
        """
        # `CREATE TABLE IF NOT EXISTS` leaves an existing table exactly as it
        # was, so a column added to the schema never appears in a store
        # created without it. Each ALTER is guarded by the table's own
        # column list rather than a version number, which keeps this
        # idempotent and independent of how the database got here.
        for table in ("patients", "studies", "instances"):
            columns = {row[1] for row in conn.execute(
                f"PRAGMA table_info({table})").fetchall()}
            if "phi_status" not in columns:
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN phi_status TEXT")
            # The policy each status was recorded under. **No back-fill**,
            # for `value_count`'s and `shift_provenance`'s reason: an
            # existing row cannot know which configuration its scan ran
            # under, and an `UPDATE ... SET phi_policy = <the policy in
            # force>` would fabricate exactly that fact. NULL beside a
            # status reads "recorded without a policy": the status is
            # restored as recorded, and an export of it says so.
            for column in ("phi_policy", "phi_policy_base"):
                if column not in columns:
                    conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
            # The status an edit left stale. NULL for every existing row
            # -- which recorded a stale status as UNSCANNED and so cannot
            # say which were stale -- and for any entity not stale; no
            # back-fill, for `phi_policy`'s reason.
            if "phi_status_edited" not in columns:
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN phi_status_edited TEXT")

        # `loss_scope` on audit_log. A DATA_LOSS row written before this
        # column existed reads NULL, and NULL is ungraded: the scope says
        # what kind of element was dropped, and the only place that ever
        # knew is the emitter that has long since run. Do not back-fill it
        # by parsing `details`: that is the coupling the column avoids.
        audit_columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(audit_log)").fetchall()}
        if "loss_scope" not in audit_columns:
            conn.execute("ALTER TABLE audit_log ADD COLUMN loss_scope TEXT")

        # `element_tag` on audit_log, by the same argument. A
        # SCAN_GAP row written before this column reads NULL, and NULL
        # is unresolved: nothing here can know which element it named,
        # so the report says so and the run keeps its REVIEW_REQUIRED
        # rather than being graded on a guess.
        if "element_tag" not in audit_columns:
            conn.execute("ALTER TABLE audit_log ADD COLUMN element_tag TEXT")

        # `source_path` on instances. Rows predating the column read
        # NULL; for an un-redacted instance `Instance.__post_init__`
        # re-derives it from `file_path` on load, so only instances
        # redacted before the column existed stay without provenance --
        # their `file_path` was cleared before anything recorded it, and
        # nothing here can recover it.
        instance_columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(instances)").fetchall()}
        if "source_path" not in instance_columns:
            conn.execute("ALTER TABLE instances ADD COLUMN source_path TEXT")

        # `date_shifted` on studies. SHIFT_DATE sets the flag and the WFDB
        # exporter reads it to decide whether the header's date comment
        # may say "de-identified", so it must survive a save and reload.
        # Rows predating the column read NULL, which hydrates as False --
        # correct, not a loss: such a row never recorded whether a date
        # was shifted, and a provenance claim the store cannot back must
        # not be fabricated (the same direction the exporter's own
        # comment enforces).
        study_columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(studies)").fetchall()}
        if "date_shifted" not in study_columns:
            conn.execute("ALTER TABLE studies ADD COLUMN date_shifted INTEGER")

        # `shifted_study_date` on studies. `date_shifted` above says a
        # shift ran; this says what it produced, so `_scan_study` can
        # tell the shift's own output from a fresh original assigned
        # over `study_date` -- which the flag alone cannot.
        #
        # Read with the flag, NULL is unambiguous and needs no second
        # provenance column:
        #
        #   False / NULL   never shifted         -> raise if there is a date
        #   True  / string shifted, recorded     -> vouched while it matches
        #   True  / NULL   shifted, unrecorded   -> no finding (nothing in
        #                                           an existing store
        #                                           changes)
        #   False / string a hand-edited or partially-written row -> the
        #                                           record wins, being the
        #                                           more specific claim
        #
        # The instance half has its own `shift_provenance` column because
        # `Instance.date_shifted` is not persisted, so an instance row
        # carries no witness at all. A study row carries one; the two
        # halves are deliberately not symmetrical.
        if "shifted_study_date" not in study_columns:
            conn.execute(
                "ALTER TABLE studies ADD COLUMN shifted_study_date TEXT")

        # `shift_provenance` on instances. The scan decides per value
        # whether a date has already been shifted, reading the record
        # `SHIFT_DATE` writes; an instance written before those records
        # existed carries none, and reading "no record" as "not shifted"
        # would make the next audit() raise every already-shifted date in
        # the store and anonymize() shift each one a second time.
        #
        # So NULL is **not** "not shifted" here, and this is the one
        # migration in this file where the absent column is the *unsafe*
        # reading. NULL means "this row predates per-value records", and
        # such an instance keeps the entity-level rule (once the date is
        # shifted, its values are not re-examined) for the values it
        # already holds. 'recorded' means its records are the whole truth.
        #
        # No back-fill, for the same reason `value_count` has none: a
        # legacy row cannot know which of its dates were shifted, and
        # an `UPDATE ... SET shift_provenance = 'recorded'` sweep would
        # answer for exactly the rows that have no answer -- which is
        # the fabrication this column exists to prevent.
        if "shift_provenance" not in instance_columns:
            conn.execute(
                "ALTER TABLE instances ADD COLUMN shift_provenance TEXT")

        # `value_count` on instance_attributes. The tier is one row per
        # value atom, so without an arity a one-element list would reload
        # as a scalar and an empty one as an absent tag. The column
        # carries the container's length on every row of an element,
        # denormalized exactly as `value_rep` is.
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

        A patient whose id has exactly the unkeyed pseudonym's shape, or
        any of whose dates was shifted (a study's `date_shifted`, or a
        `__shifted__` record at any depth), is classed unkeyed: a keyed
        offset would put a second offset on its dates. Every other patient
        is keyed. Runs on every open and touches only NULL rows; a class
        once written is never revisited.

        Args:
            conn (sqlite3.Connection): An open connection in the caller's
                transaction.
        """
        # A NULL comes only from a build that predates the column --
        # including one run against this store after a newer build opened
        # it, which is why this runs on every open. Never re-derive a
        # written class: after a keyed shift is saved, "has a shifted date"
        # is true of keyed patients too, so re-deriving would downgrade
        # them.
        #
        # The id arm is the exact shape, never a prefix: a prefix would
        # class a keyed patient's 29-character pseudonym unkeyed if an older
        # build ever wrote its row, and the unkeyed arm would then seed that
        # patient's offset on characters of the pseudonym the export
        # carries. A non-hex `ANON_` id that was shifted is still caught by
        # the two witness arms. `instr` over the serialized JSON finds a
        # nested item's `__shifted__` record too, because nested items are
        # serialized into the same blob.
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
        """Copy `instances.pixel_*` references into `instance_blobs`.

        Idempotent: a reference already in `instance_blobs` is left as it
        is. The `instances` columns are left in place.

        Args:
            conn (sqlite3.Connection): An already-open connection, in the
                caller's transaction; acquiring one here would deadlock on
                a `:memory:` store.
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
        """A lazy pixel loader for one sidecar frame.

        Args:
            offset (int): The frame's byte offset in the sidecar.
            length (int): Its stored length.
            alg (str): Its compression.
            instance (Instance): The instance whose descriptors shape the
                frame.
            pixel_hash (str, optional): The SHA-256 the frame must match.

        Returns:
            SidecarPixelLoader: The loader.
        """
        # Use instance to populate primitives
        return SidecarPixelLoader(self.sidecar_path, offset, length, alg, instance=instance, pixel_hash=pixel_hash)

    def _wire_nested_pixel_refs(self, instance, rows):
        """Restore an Instance's nested pixel references.

        Every row becomes a `NestedPixelRef`, even when its item is no
        longer in the graph; the export decides whether it is carried or
        reported as lost. A row whose kind cannot be parsed is skipped with
        a warning.

        Args:
            instance (Instance): The hydrated instance.
            rows: `instance_blobs` rows whose kind matches `pixels:%`, or
                None when this instance has none.
        """
        # Wired unconditionally, without resolving the path against the
        # graph: the export post-pass is the single place that decides
        # carried-or-reported, and discarding a ref here would mean no
        # `DATA_LOSS` row for bytes that are in the store and cannot be
        # placed. References, not loaders (see `io_handlers.NestedPixelRef`):
        # a loader built now would reshape against the geometry the graph
        # has *now*, and export re-checks it because the graph may move.
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
            # left it, so this reads what ingest recorded. A ref whose
            # item is already gone gets None, which
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

        Also prunes multiplex items that have no samples (see
        `_prune_hollow_multiplex_items`). A blob with no Waveform Sequence
        to read its geometry from gets no loader, with a warning.

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
        """Drop Waveform Sequence items beyond the first, which have no samples.

        A store indexed by an older build can hold one Waveform Sequence
        (5400,0100) item per multiplex group while the sidecar holds group
        0's samples alone. Without this the export would declare a
        multiplex group with no Waveform Data (5400,1010), a Type 1
        element. Waveform annotations referencing the pruned groups are
        dropped or trimmed with them.

        The graph changes without an edit: the revision does not move, the
        stored status survives, and the store is untouched until the next
        save. Logs a warning naming the instance and the remedy (no audit
        row), on every open of an unhealed store.

        Args:
            instance (Instance): The hydrated instance, attributes and
                sequences restored.
        """
        # Pruned at hydration rather than at export: the graph is what every
        # consumer reads -- the DICOM writer, the WFDB record, the annotation
        # bridge, the PHI scan -- and a writer that quietly drops items is a
        # second answer to "which multiplex groups does this record have".
        # No audit row: the ingesting session already wrote the DATA_LOSS
        # row, and this heals the graph to agree with it. Annotations go
        # through the filter ingest uses, or a dangling ordinal is left.
        # Direct container mutation, never `set_attr`, so the prune does not
        # look like an edit (the invariant `_apply_vertical_attributes`
        # states).
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

        Returns once the queue has been seen empty under
        `_audit_write_lock`; safe to call from any thread.
        """
        # The only place rows leave `audit_queue`, and the lock is what
        # makes `flush_audit_queue` a barrier rather than a hopeful drain.
        # Rows leave the queue only under `_audit_write_lock` and are in the
        # database before it is released, so a row is never owned by a
        # local variable a reader cannot see. Do not move the `get()` out
        # from under the lock: a row taken off the queue but not yet
        # written is in neither the queue nor the table, and a reader that
        # "flushed" would find nothing to do and select without it.
        #
        # `log_audit_batch` must never acquire `_audit_write_lock`: it is
        # called here *while holding it*, and `threading.Lock` is not
        # reentrant, so a defensive acquire would self-deadlock.
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
        """Stop the audit worker and settle the audit log.

        Every row queued before the call is written. A store that created
        its own temporary sidecar (a `:memory:` store, not a pickled clone)
        also deletes it and its two lock files.
        """
        self._stop_event.set()
        # Wake the worker now instead of letting it wait out its 1.0 s
        # tick, so the join below rarely has to fire at all.
        self._audit_wakeup.set()
        if self._audit_thread.is_alive():
            self._audit_thread.join(timeout=2.0)
        # A timed-out join loses no rows: this waits out any in-flight
        # write on the lock and drains the rest itself.
        self.flush_audit_queue()

        # Only the store that created a `:memory:` temp sidecar removes
        # it -- never a pickled clone (flag dropped on pickle) and never
        # a file-backed store (its sidecar is data, and its lock files
        # are stable paths other processes may be polling). Without this,
        # each session leaks three temp files. `FileNotFoundError` is
        # expected for a lock file no acquisition ever created.
        if self._owns_temp_sidecar:
            for path in (self.sidecar_path, self._gate_path(),
                         self._pass_lock_path()):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass

    def flush_audit_queue(self):
        """Settle the audit log.

        Returns only when every row enqueued before this call is readable from
        `audit_log`. A barrier, not a poll, with no timeout; the wait is at
        most one in-flight `log_audit_batch`. Never call it from inside a
        `_get_connection` block: on a `:memory:` store that inverts the lock
        order and deadlocks.
        """
        # No timeout on purpose: a compliance read that gave up could not
        # be told from one that found nothing. Producers never hold the
        # lock, so no volume of logging can extend a single acquisition.
        self._drain_and_write()

    def log_audit(self, action_type: str, entity_uid: str, details: str,
                  loss_scope: Optional[str] = None,
                  element_tag: Optional[str] = None):
        """Queue an action for the audit log.

        Returns at once; the audit worker, or the next reader's flush,
        writes the row. Safe from any thread.

        Args:
            action_type (str): e.g. 'EXPORT', 'ERROR', 'DATA_LOSS'.
            entity_uid (str): The instance (or path) the action concerns.
            details (str): Prose for the human reading the report.
            loss_scope (str, optional): For `DATA_LOSS` only:
                `io_handlers.LOSS_SCOPE_PRIVATE`, `LOSS_SCOPE_STANDARD`
                or `LOSS_SCOPE_SIGNAL`. `generate_report` grades on it; the
                caller passes it because only the caller still holds the tag.
            element_tag (str, optional): For `SCAN_GAP` only: the
                `gggg,eeee` the parse gate refused. `generate_report`
                resolves it against the object graph to say whether the
                element is still held for export.
        """
        # Push to queue instead of writing directly. Producers take
        # neither lock and are never blocked by a database write.
        self.audit_queue.put(
            (action_type, entity_uid, details, loss_scope, element_tag))
        self._audit_wakeup.set()

    def get_audit_summary(self) -> Dict[str, int]:
        """Returns an aggregated summary of actions from the audit log.

        Reads through the `flush_audit_queue` barrier, so every row enqueued
        before the call is counted.

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
        """Every `ERROR` and `WARNING` audit row, oldest first.

        Flushes the audit queue first, so every row enqueued before the
        call is included.

        Returns:
            List[tuple]: `(timestamp, action_type, details)` per row; empty
                if the query fails.
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
        """Retrieves every `DATA_LOSS` entry, with the scope it was
        recorded under.

        Separate from `get_audit_errors` because the scopes grade differently:
        a loss scoped `PRIVATE` or `SIGNAL` takes `validation_status` to
        `REVIEW_REQUIRED`; one scoped `STANDARD` leaves it at `PASS`. Every
        loss is reported under "3.1 Data Loss", never under "Exceptions &
        Errors".

        A row whose `loss_scope` is NULL predates the column and cannot be
        graded; it is reported and left at `PASS`.

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

        Separate from `get_audit_losses` because it is a different claim. A loss
        says an element was dropped at ingest and cannot reach the output; this
        says an element was kept whole and the scan could not read what is
        inside it.

        The row states ingest-time knowledge only. Whether the element reaches
        the exported file is decided later, by `remove_private_tags`, and
        `generate_report` resolves that against the object graph.

        No `loss_scope` column: only an odd-group tag reaches the parse gate,
        so these are private by construction. `element_tag` is selected
        instead, and is NULL for a row written before that column existed.

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

        A scan gap says an element could not be *read*; this says an element was
        read, a remediation was proposed for it, and the remediation did not
        run, so the value is still in the graph and will reach the exported
        file. The reason is in `details`.

        Flushes first, like every other audit reader, so a row still in the
        queue is counted.

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

        The rows themselves are unrecoverable (see `log_audit_batch`). A
        non-zero count means the audit table under-states what happened, and
        `generate_report` grades it like an exception.

        Flushes first, like every other audit reader, so a row still in the
        queue is counted if its write fails.

        Returns:
            int: Rows dropped over this store object's lifetime.
        """
        self.flush_audit_queue()
        with self._audit_drop_lock:
            return self._audit_rows_dropped

    def check_unsafe_attributes(self) -> List[tuple]:
        """Instances whose stored attributes say Burned In Annotation is YES.

        Returns:
            List[tuple]: `(sop_instance_uid, file_path, details)` per
                instance; empty if the query fails.
        """
        unsafe = []
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                # A text search in the JSON. It relies on `json.dumps`'s
                # default `": "` separator between key and value.
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

        Detects stores holding a guessed geometry for a multi-frame grayscale
        instance (RGB, 3 samples, swapped axes), which exports garbage while
        every later step behaves correctly on the wrong descriptors. Nothing is
        repaired: the sidecar's bytes are shape-free. Re-ingest from source, or
        export with `verify_readback=True`; `DicomSession.__init__` logs a
        warning from this result.

        The check is arithmetic and exact: Rows x Columns x SamplesPerPixel x
        NumberOfFrames x bytes-per-sample (2 above 8 bits allocated, else 1)
        must equal the stored frame length.

        **Scope: frames stored uncompressed only.** A zlib frame's stored length
        is post-compression, and decompressing every frame would read the whole
        sidecar on every open. A frame whose `compress_alg` is NULL is skipped
        too: its encoding is unrecorded. Damage behind a compressed frame is
        caught where the bytes are decoded, by `verify_readback` at export.

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

            # `SidecarPixelLoader`'s dtype bucketing (`uint16 if bits > 8
            # else uint8`), not BitsAllocated/8: the sidecar holds
            # `pixel_array.tobytes()`, so a 1-bit Segmentation is stored
            # expanded to uint8.
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
        """Insert audit rows now, in one transaction, all with one timestamp.

        A batch that fails to insert, for any reason, sqlite or not, is
        *dropped and counted* in `get_audit_drops()`, and logged; it is never
        retried and never raised. Never acquires `_audit_write_lock`, so it
        may be called with that lock held.

        Args:
            entries (List[tuple]): `(action_type, entity_uid, details,
                loss_scope, element_tag)` per row; `loss_scope` is None
                except on `DATA_LOSS` rows and `element_tag` None except on
                `SCAN_GAP` rows. Empty writes nothing.
        """
        # Retrying would mean holding the rows somewhere no reader's barrier
        # sees, or looping on a write that always fails; the count is what
        # reaches a reader instead.
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
        """Reconstructs the entire object graph from the database.

        Every entity comes back with its stored PHI status and policy, lazy
        pixel and waveform loaders wired to the sidecar, and no unsaved
        changes. Writes at most one `WARNING` audit row each for instances
        and studies without per-value date records and for patients under
        the unkeyed jitter scheme, and logs how many statuses carry no
        policy. A database error is logged and gives an empty list.

        Returns:
            List[Patient]: Every stored patient, with its subtree; empty for
                a file store whose database file does not exist.
        """
        patients = []
        if self.db_path != ":memory:" and not os.path.exists(self.db_path):
            return patients

        try:
            with self._get_connection() as conn:
                cur = conn.cursor()

                # Fetch every table whole and stitch in memory: faster in
                # SQLite than N+1 queries.

                # 1. Fetch all
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
                    stored_statuses.append((p, r))

                st_map = {}
                legacy_studies = 0
                for r in st_rows:
                    st = Study(r['study_instance_uid'], _as_loaded_date(r['study_date']))
                    # NULL (a row from before the column) and 0 both read
                    # False: only a store that recorded the shift may
                    # claim one.
                    st.date_shifted = bool(r['date_shifted'])
                    # What the shift produced, or NULL for a row written
                    # before per-value records -- which with the flag set
                    # means "shifted, value unknowable" and keeps the
                    # study-level rule for this study. Assigned, not
                    # recorded through `record_date_shift`, because
                    # hydration restores a state rather than making an
                    # edit.
                    st._shifted_study_date = r['shifted_study_date']
                    if st.date_shifted and st._shifted_study_date is None:
                        legacy_studies += 1
                    st_map[r['id']] = st
                    stored_statuses.append((st, r))
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
                # reason. `LIKE` rather than `GLOB` is sound because
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
                # kept out of `attributes_json`; without reading them back,
                # `remove_private_tags=False` would hold only until the
                # session closed. Pre-fetched here for the same reason as
                # `wave_refs` above: per-instance would be one query per
                # instance on every session open.
                vertical_vrs = {}
                vertical = self.load_vertical_attributes_bulk(
                    conn=conn, vrs=vertical_vrs)

                se_map = {}
                # Which study each series belongs to, so the instance loop
                # below can read its parent's `date_shifted`. The
                # loop is flat here, unlike `load_patient`'s, so the link
                # has to be kept rather than being in scope.
                se_study = {}
                for r in se_rows:
                    se = Series(r['series_instance_uid'], r['modality'], r['series_number'])
                    # Same rule as ingest and `load_patient`, by
                    # construction: keep it `Equipment.from_parts`, never
                    # a hand-copied predicate.
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
                    # Legacy date provenance, read off the row.
                    # NULL alone only says the row predates per-value
                    # records; the **study's** flag is the only persisted
                    # evidence anywhere that a shift ever ran, because
                    # `Instance.date_shifted` has no column. An old-store
                    # instance under an unshifted study has nothing to
                    # protect, and marking it legacy would exempt its
                    # values from the per-value rule once a later pass
                    # shifts its study. Studies are hydrated above, so
                    # the flag is set before this reads it; `load_patient`
                    # does the same from its nested loop.
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
                    # in every session that reopens the store.
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

                        # The stored hash goes to the loader, explicitly. A
                        # loader built with none has no integrity check, and
                        # the fallback to `inst._pixel_hash` finds nothing
                        # here: hydration never sets it. Without the check,
                        # a reopened session would read another frame's
                        # bytes at this offset as this instance's pixels.
                        # Passed rather than set on the instance: an
                        # explicit hash has no ordering to get wrong.
                        # `load_patient` below does the same; change them
                        # together.
                        inst._pixel_loader = self._create_pixel_loader(
                            r['pixel_offset'], r['pixel_length'], r['compress_alg'], inst,
                            pixel_hash=r['pixel_hash'])

                    self._wire_waveform_loader(inst, wave_refs.get(r['sop_instance_uid']))
                    self._wire_nested_pixel_refs(
                        inst, nested_refs.get(r['sop_instance_uid']))

                    if r['series_id_fk'] in se_map:
                        se_map[r['series_id_fk']].instances.append(inst)

                    stored_statuses.append((inst, r))

            self.logger.info(f"Loaded {len(patients)} patients from {self.db_path}")
            self._report_legacy_shift_provenance(legacy_instances, legacy_studies)
            self._report_unkeyed_scheme(patients)

            # The row that was loaded is the row the status was written for,
            # so the stored conclusion applies to this revision. Recorded
            # before marking persisted, because recording advances the
            # revision.
            self._report_statuses_without_a_policy(
                self._restore_statuses(stored_statuses))

            # Mark all loaded data as clean so we don't save it back immediately
            for p in patients:
                p.mark_subtree_persisted()
            return patients

        except sqlite3.Error as e:
            self.logger.error(f"Failed to load PDF from DB: {describe_exception(e)}")
            traceback.print_exc()
            return []

    def load_patient(self, patient_uid: str) -> Optional[Patient]:
        """Loads a single patient and their subtree by Patient ID.

        Hydrates as `load_all` does, and writes the same notices for this
        patient alone. A database error is logged and gives None.

        Args:
            patient_uid (str): The Patient ID to load.

        Returns:
            Optional[Patient]: The patient, or None if no row has that ID.
        """
        if self.db_path != ":memory:" and not os.path.exists(self.db_path):
            return None

        try:
            with self._get_connection() as conn:
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
                stored_statuses = [(p, p_row)]
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
                # reason. `LIKE` rather than `GLOB` is sound because
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
                    # note there.
                    st.date_shifted = bool(st_r['date_shifted'])
                    # Same rule as load_all's; see the note there.
                    st._shifted_study_date = st_r['shifted_study_date']
                    if st.date_shifted and st._shifted_study_date is None:
                        legacy_studies += 1
                    st_pk = st_r['id']
                    stored_statuses.append((st, st_r))

                    # Fetch Series
                    se_rows = cur.execute(
                        "SELECT * FROM series WHERE study_id_fk = ?", (st_pk,)).fetchall()
                    for se_r in se_rows:
                        se = Series(
                            se_r['series_instance_uid'],
                            se_r['modality'],
                            se_r['series_number'])
                        # Same rule as ingest and `load_all`.
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
                            # it keeps the study-level rule for them.
                            # `st` is in scope here, so no map is needed.
                            if (r['shift_provenance'] is None
                                    and st.date_shifted):
                                inst._legacy_shift_provenance = True
                                legacy_instances += 1
                            # See load_all: after construction, so the
                            # stored origin wins, and it is the only
                            # thing that restores it for a redacted
                            # instance whose `file_path` is NULL.
                            if r['source_path']:
                                inst.source_path = r['source_path']
                            # Restore extra attributes, as load_all does.
                            if r['attributes_json']:
                                try:
                                    attrs = json.loads(
                                        r['attributes_json'], object_hook=isocenter_json_object_hook)
                                    self._deserialize_into(inst, attrs)
                                except (json.JSONDecodeError, TypeError) as exc:
                                    # Logged, never silent: the instance
                                    # loads with no attributes at all.
                                    self.logger.error(
                                        "Could not decode stored attributes "
                                        "for instance %s: %s",
                                        r['sop_instance_uid'], describe_exception(exc))

                            # Wire up Sidecar. Duplicates load_all's loader
                            # construction; change them together.
                            if r['pixel_offset'] is not None and r['pixel_length'] is not None:
                                # With the stored hash, as `load_all` does
                                # and for its reason.
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
                            stored_statuses.append((inst, r))

                        st.series.append(se)
                    p.studies.append(st)

                # Ahead of the status loop and of `mark_subtree_persisted`,
                # matching `load_all`. Not load-bearing on its own --
                # `_apply_vertical_attributes` advances no revision -- but
                # keep the order: it makes a `set_attr` slipping in here
                # survivable rather than a silent UNSCANNED regression; the
                # invariant itself lives on that helper.
                vertical_vrs = {}
                vertical = self.load_vertical_attributes_bulk(
                    [i.sop_instance_uid for i in hydrated_instances], conn=conn,
                    vrs=vertical_vrs)
                for inst in hydrated_instances:
                    self._apply_vertical_attributes(
                        inst, vertical.get(inst.sop_instance_uid, {}),
                        vertical_vrs.get(inst.sop_instance_uid, {}))

                # Same as load_all's, and the same "per load" call-site
                # caveat as `_report_legacy_shift_provenance`.
                self._report_statuses_without_a_policy(
                    self._restore_statuses(stored_statuses))

                self._report_legacy_shift_provenance(legacy_instances,
                                                     legacy_studies)
                self._report_unkeyed_scheme([p])
                p.mark_subtree_persisted()
                return p
        except sqlite3.Error as e:
            self.logger.error(f"Failed to load patient: {describe_exception(e)}")
            return None

    #: What a load says once when it finds rows written before
    #: per-value date records existed. One `WARNING` audit row and one
    #: log line per load, not per instance: a 100k-instance store would
    #: otherwise flood both channels, and the fact is about the store,
    #: not about any one row.
    #:
    #: A `WARNING` row because `generate_report` grades a run with
    #: section-4 rows `REVIEW_REQUIRED`, which is the honest grade for a
    #: session that cannot answer the question for part of its graph --
    #: and it means the limitation reaches the compliance report rather
    #: than only a console the operator scrolled past.
    #:
    #: Three things the wording does deliberately: it names *which*
    #: guarantee is missing (the per-value one, not "never shifted
    #: twice"), it names the failure direction (a real date may survive;
    #: a double shift can never happen), and it names the remedy.
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

    @staticmethod
    def _restore_statuses(stored_statuses) -> int:
        """Restore each hydrated entity's status under its stored policy.

        The status and its policy are restored as recorded, never
        reinterpreted against the policy in force; a row with no policy
        keeps None. A row whose status an edit left stale
        (`phi_status_edited`) is recorded and then its revision moved, so
        it reads UNSCANNED and grade condition 8 counts it until `audit()`
        reads it. Call before `mark_subtree_persisted()`, so the entities
        read clean.

        Args:
            stored_statuses: `(entity, row)` pairs, each patient, study and
                instance with the row it was built from.

        Returns:
            int: How many statuses carry no policy (UNSCANNED and stale
                ones aside).
        """
        # Nothing is reinterpreted against the policy in force, which a
        # load cannot know (`Session(db)` hydrates before any
        # `load_config`), and no policy is made up for a row without one.
        # Policies are interned per load, so ten thousand instances
        # scanned under one policy share one object.
        interned = {}
        legacy = 0
        for entity, row in stored_statuses:
            status = _phi_status_from_stored(row['phi_status'])
            fingerprint = row['phi_policy']
            policy = None
            if status is PhiStatus.UNSCANNED:
                # A status an edit left stale: restored as recorded and
                # then left behind, so the entity reads UNSCANNED and
                # condition 8 still counts it -- it grades until `audit()`
                # reads it, as it did before the save. No policy: a stale
                # status reads none, and none was stored.
                edited = _stale_status_from_stored(row['phi_status_edited'])
                if edited is not None:
                    entity.record_phi_status(edited, policy=None)
                    entity.mark_modified()
                    continue
            elif isinstance(fingerprint, str) and fingerprint:
                base = row['phi_policy_base']
                key = (fingerprint, base)
                policy = interned.get(key)
                if policy is None:
                    policy = interned[key] = ScanPolicy(
                        fingerprint, base if isinstance(base, str) else "")
            else:
                legacy += 1
            entity.record_phi_status(status, policy=policy)
        return legacy

    #: What a load says once when statuses in the store carry no policy.
    #: A log line and no audit row: a row per load would grade every
    #: report over the store REVIEW_REQUIRED with no export, and the
    #: export that writes such instances already writes its own row.
    _STATUSES_WITHOUT_A_POLICY_NOTICE = (
        "{count} in this store {carry} no recorded policy: written before "
        "1.0, which recorded none, or remediated from findings that are "
        "not a whole audit() report. Which configuration the scan ran "
        "under is not known. They are restored as recorded, and an export that writes "
        "them says so. To record a policy, run audit() under the "
        "configuration you mean, then save() (#555).")

    def _report_statuses_without_a_policy(self, count: int):
        """Log one WARNING with how many statuses carry no policy.

        One line per call; see `_report_legacy_shift_provenance` for why
        that is one per load.

        Args:
            count (int): The number to report; 0 logs nothing.
        """
        if not count:
            return
        self.logger.warning(self._STATUSES_WITHOUT_A_POLICY_NOTICE.format(
            count=(f"{count} PHI status" if count == 1
                   else f"{count} PHI statuses"),
            carry="carries" if count == 1 else "carry"))

    def _report_legacy_shift_provenance(self, instances: int, studies: int = 0):
        """Say that part of this graph has no per-value shift records.

        Those instances and studies keep the study-level rule: once the
        study's date is shifted, their SHIFT/JITTER values are not
        re-examined. Writes one log line and one `WARNING` audit row
        (`_LEGACY_SHIFT_NOTICE`) covering both counts, per call. The
        contract is one notice per *load*: `Session` loads through
        `load_all` once, so a caller loading patients one at a time must
        accumulate its counts and call this once.

        Args:
            instances (int): Instances without per-value records.
            studies (int): Studies shifted without a recorded result.
        """
        # One notice, not two: the instance half and the study half are the
        # same limitation at two levels, and two rows about one store would
        # read as two problems. Nothing here enforces "per load"; it reports
        # whatever counts it is handed, and `load_patient` calls it too.
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
    #: scheme's pseudonym and date offset. Its own row,
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
        """The unkeyed-scheme notice for these counts, or `""`.

        Args:
            legacy (int): Patients classed `JITTER_SCHEME_UNKEYED`.
            pseudonyms (int): Keyed patients carrying an unkeyed pseudonym.

        Returns:
            str: The notice, ending with the remedy; `""` when both are 0.
        """
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
        unkeyed pseudonym carried in from an earlier export (an id
        already `ANON_` is never replaced, so it is exported as it is).
        Writes one log line and one `WARNING` audit row when either is
        non-zero.

        Args:
            patients (List[Patient]): The patients just loaded.
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

    # A project secret stays in the store that generated it. There is no
    # way to write one out or to load one in: a secret file would recover
    # every date of every store that loaded it, and a later batch for the
    # same patients is served by ingesting into the same store. A copy of
    # the store file is the same store; nothing detects or refuses one.

    _MISSING_SECRET_REFUSAL = (
        "This store holds dates shifted under a project secret it no "
        "longer has ({n}). A new secret would give {those} a second date "
        "offset, so audit() and anonymize() refuse. The secret cannot be "
        "restored from outside the store: re-ingest the source files into "
        "a new store (#716).")

    _FOREIGN_PSEUDONYM_NOTICE = (
        "{n} in this store carr{ies} {a}`ANON_` pseudonym{s} minted under a "
        "different project secret{generated}. {their} dates are shifted "
        "with this store's offsets, not the ones {their_lc} source store "
        "used. If this data belongs to an existing project, ingest it "
        "into that project's store.")

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

        Read from the store on every call and never cached, so no pickle
        of this store carries it. The evidence is the store's rows, which is
        why `Session.audit()` drains the persistence manager first.

        With no secret row:

        - **Refused** (`RuntimeError`, nothing created) when a keyed
          patient has any shifted date: such a patient's dates were
          shifted under a secret this store no longer has, and a new one
          would give them a second offset.
        - **Refused** the same way when instances carry a replaced SOP
          Instance UID (`_replaced_uid_evidence`): a new secret would
          replace them a second time.
        - **Generated, with a `WARNING` row**, when keyed `ANON_`
          pseudonyms are present but nothing was shifted: an export from
          another project ingested into a fresh store. The split is
          across stores, not inside this one.
        - **Generated silently** otherwise.

        With `diagnose` (what `audit()` passes; `anonymize()` does not,
        so `anonymize(audit())` writes each notice once), a `WARNING` row
        also names keyed pseudonyms that do not verify under the secret,
        ids re-ingested from an unkeyed-scheme export, and patients whose raw
        data arrived after this store de-identified them under the
        unkeyed scheme.

        Generation is `INSERT OR IGNORE` then `SELECT`, so two sessions
        reaching first use on one database at once converge on one
        secret.

        Args:
            diagnose (bool): Write the per-audit diagnostic notices too.

        Returns:
            bytes: The 32-byte project secret.

        Raises:
            RuntimeError: There is no secret row and the store holds
                shifted dates of keyed patients, or replaced UIDs.
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
            replaced = self._replaced_uid_evidence()
            if replaced:
                raise RuntimeError(self._MISSING_SECRET_UID_REFUSAL.format(
                    n=f"{replaced} instance{'' if replaced == 1 else 's'}"))
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

    #: The refusal for a store whose UIDs were replaced under a secret it
    #: no longer has. Under `basic` Study Date is
    #: emptied, so no shifted date is left to refuse on; the UIDs are the
    #: evidence instead.
    _MISSING_SECRET_UID_REFUSAL = (
        "This store holds replaced UIDs derived under a project secret it "
        "no longer has ({n}). A new secret would not recognise them and "
        "would replace them a second time, a second UID for each of those "
        "instances, so audit(), anonymize() and redact() refuse. The secret "
        "cannot be restored from outside the store: re-ingest the source "
        "files into a new store (#716, #544).")

    def _replaced_uid_evidence(self) -> int:
        """How many instances carry a SOP Instance UID of the shape this
        library mints (`2.25.` and an RFC 9562 version-8 UUID) beside the
        UID they were ingested under (`SOURCE_SOP_UID_ATTR`): a UID replaced
        by `anonymize()` or `redact()`.

        Judged by shape, since the secret that would verify them is what is
        missing. A UID under pydicom's root (`1.2.826.0.1.3680043.8.498.`)
        is not counted.

        Returns:
            int: How many such instances the store holds.
        """
        # A UID `uids.generated_uid` makes for an absent Study or Series has
        # the shape too, but no source record beside it, and it is not a
        # SOP Instance UID, so it is never counted here.
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT sop_instance_uid FROM instances WHERE sop_instance_uid "
                "LIKE '2.25.%' AND instr(attributes_json, ?) > 0",
                (f'"{entities.SOURCE_SOP_UID_ATTR}"',)).fetchall()
        return sum(1 for (uid,) in rows if _has_minted_uid_shape(uid))

    def _project_secret_if_present(self) -> Optional[bytes]:
        """The project secret if this store holds one, else None.

        Never creates one and writes no notice, for readers that must not
        change the store (`ingest()` and an export subset recognising UIDs
        this store replaced). A store with no secret has replaced nothing.

        Returns:
            Optional[bytes]: The secret, or None.
        """
        with self._get_connection() as conn:
            return self._read_project_secret(conn)

    def _insert_project_secret(self, secret: bytes, origin: str):
        """Insert `secret` unless a row exists.

        Args:
            secret (bytes): The secret to store.
            origin (str): Its `project_secret.origin` value.

        Returns:
            Tuple[bool, bytes]: Whether this call inserted, and the secret
                the store holds afterwards -- the winner when another session
                inserted first.
        """
        # Never `REPLACE`: a replaced secret is a second offset for every
        # patient already shifted under the first.
        with self._get_connection() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO project_secret "
                "(id, secret_hex, origin, created_at) VALUES (1, ?, ?, ?)",
                (secret.hex(), origin, datetime.now().isoformat()))
            inserted = cur.rowcount == 1
            return inserted, self._read_project_secret(conn)

    #: `project_secret.origin` values. A store format, like the scheme
    #: names: `_project_secret_for_use` reads the unverified one back at
    #: every `audit()`, because nothing else in the store records that
    #: the secret was never checked. Nothing in this library writes
    #: `loaded` or `loaded-unverified`; both stay so that a store holding
    #: an unverified loaded secret keeps warning, and grading
    #: REVIEW_REQUIRED.
    _ORIGIN_GENERATED = "generated"
    _ORIGIN_LOADED = "loaded"
    _ORIGIN_LOADED_UNVERIFIED = "loaded-unverified"

    # Written at every audit by a store whose secret origin is
    # `loaded-unverified`. Its last sentence is the only remedy there is:
    # no secret can be loaded from outside the store. Rows already written
    # keep their own text.
    _UNVERIFIED_SECRET_NOTICE = (
        "This store's project secret could not be verified when it was "
        "loaded. If it is not the secret this store's earlier dates were "
        "shifted under, every date shifted since the load carries a "
        "different offset from the dates of the same patients shifted "
        "before it, and the store cannot tell which. The secret cannot be "
        "checked or replaced from outside the store: where those offsets "
        "matter, re-ingest the source files into a new store (#716).")

    def _serialize_item(self, item: Instance) -> Dict[str, Any]:
        """Serializes an Instance to the dictionary stored as `attributes_json`.

        Holds the attributes plus, where present, the root-only keys
        `__vrs__` (VRs of private `bytes` values), `__shifted__`,
        `__remediated__`, `__locked__`, and `__sequences__` (each item
        through `_serialize_dicom_item`).

        Args:
            item (Instance): The instance to serialize.

        Returns:
            Dict[str, Any]: A new dict; the item is not modified.
        """
        data = item.attributes.copy()
        # The root `__vrs__` holds exactly the recorded VRs of the private
        # tags whose value is `bytes`: the set
        # `_split_core_and_private` keeps here in `attributes_json`, where
        # the private-tag table and its `value_rep` column never see
        # them. Every other root private tag's VR lives in `value_rep`,
        # and copying those here too would be a second answer that can
        # disagree with it after a partial write. One home
        # per tag: `value_rep` for the vertical tier, this key for the
        # bytes beside it, the nested `__vrs__` below the root. Read
        # from the same `attributes` snapshot as the values, so a tag
        # that changed tier between two reads cannot be misfiled.
        vrs = getattr(item, "attribute_vrs", None) or {}
        binary_vrs = {tag: vrs[tag] for tag, value in data.items()
                      if tag in vrs and isinstance(value, bytes)
                      and _is_private_tag(tag)}
        if binary_vrs:
            data['__vrs__'] = binary_vrs
        # `__shifted__` here as well as in `_serialize_dicom_item`, where
        # it sits beside `__vrs__`. A date record has no other
        # home at any depth, so the root needs this key or a top-level
        # shifted date is raised and shifted again on the next load.
        if getattr(item, "_shifted_dates", None):
            data['__shifted__'] = dict(item._shifted_dates)
        # What a remediation left at each top-level tag, the root
        # only: `Instance` alone has the slot. In the same JSON as the
        # values it vouches for, so a partial write cannot store one
        # without the other.
        values = getattr(item, "_remediated_values", None)
        blank = getattr(item, "_remediated_blank", None)
        if values or blank:
            data['__remediated__'] = {"values": dict(values or {}), "blank": blank or ""}
        if item.sequences:
            seq_data = {}
            for tag, seq in item.sequences.items():
                items_list = []
                for seq_item in seq.items:
                    # Sequence items are plain DicomItems; the root-only
                    # keys above are not theirs.
                    items_list.append(self._serialize_dicom_item(seq_item))
                seq_data[tag] = items_list
            data['__sequences__'] = seq_data
        # The digest of the identity token this store embedded, the root
        # only, and read **after** the sequences above, which is
        # where the token itself lives. The lock stamps and then embeds;
        # a save that read the stamp first could read None, then the
        # sequences with the new token, and store a token without its
        # stamp -- this store's own token, refused as foreign on the next
        # changed-value re-lock. Read last, the save stores either the
        # old token with the old stamp, the new token with the new stamp,
        # or the old token with the new stamp, which is harmless: the
        # stamp is keyed on the token, and an old token under a new stamp
        # reads as unvouched-for only until the next save. Do not move
        # this read above `attributes.copy()`.
        locked = getattr(item, "_locked_token", None)
        if locked:
            data['__locked__'] = locked
        return data

    def _serialize_dicom_item(self, item) -> Dict[str, Any]:
        """Serializes a nested DicomItem, recursively.

        Carries the item's attributes and, where present, `__vrs__` (every
        recorded VR), `__shifted__`, `__phi__` (its PHI status, if not
        UNSCANNED) and `__sequences__`.

        Args:
            item (DicomItem): The sequence item to serialize.

        Returns:
            Dict[str, Any]: A new dict; the item is not modified.
        """
        # A nested private tag never reaches the `instance_attributes`
        # table -- it rides this JSON -- so its VR has no `value_rep` home
        # and must travel in `__vrs__` here, or an inner private tag would
        # behave differently from an outer one on the same instance. The
        # root (`_serialize_item`) emits `__vrs__` for private `bytes`
        # values only: every other root private tag's VR lives in
        # `value_rep`, and a second copy could disagree with it after a
        # partial write.
        data = item.attributes.copy()
        if getattr(item, "attribute_vrs", None):
            data['__vrs__'] = dict(item.attribute_vrs)
        # The per-value date record, the same way. A nested date is the
        # half `Instance.date_shifted` cannot speak for, so without this
        # key it comes back from the store unvouched-for and the next pass
        # shifts it again.
        if getattr(item, "_shifted_dates", None):
            data['__shifted__'] = dict(item._shifted_dates)
        # The item's PHI status, items only: a patient, study or
        # instance has its status column, and a second home here would be
        # a second answer after a partial write -- `__vrs__`'s asymmetry,
        # for `__vrs__`'s reason. The revision-checked property, never the
        # raw slot, so an item edited since its remediation stores none.
        # No policy key: an item is never scanned, so its status has none.
        # A mapping, so a key can be added inside it later; the reader pops
        # it at every depth and reads an absent key as no key.
        status = item.phi_status
        if status is not PhiStatus.UNSCANNED:
            data['__phi__'] = {"status": status.value}
        if item.sequences:
            seq_data = {}
            for tag, seq in item.sequences.items():
                items_list = [self._serialize_dicom_item(i) for i in seq.items]
                seq_data[tag] = items_list
            data['__sequences__'] = seq_data
        return data

    def _deserialize_into(self, target_item, data: Dict[str, Any]):
        """Populates `target_item` from a dict `_serialize_item` wrote.

        Restores attributes, VRs, date-shift records, remediation and lock
        stamps (where the item has a slot for them), sequences (an empty
        one included) and a nested item's PHI status. Attributes and
        records are assigned, not recorded through the entity's recording
        methods, because hydration restores a state rather than making an
        edit. The `__...__` keys are popped from `data` at every depth, so
        none becomes an attribute.

        Args:
            target_item (DicomItem): The item or instance to fill.
            data (Dict[str, Any]): The decoded dict; consumed (keys are
                popped).
        """
        sequences_data = data.pop('__sequences__', None)
        vrs_data = data.pop('__vrs__', None)
        # Popped **before** `attributes.update(data)` below, exactly as
        # `__vrs__` is: left in, the key would land in `attributes` as a
        # tag that is not a tag, and reach every reader of it -- the
        # exporter's merge and `export_dataframe(expand_metadata=True)`
        # among them.
        shifted_data = data.pop('__shifted__', None)
        # The same, at every depth though only the root writes it: a
        # hand-edited nested key must not become a tag either.
        remediated_data = data.pop('__remediated__', None)
        # And the lock's stamp, popped at every depth for the same reason
        # and assigned only where there is a slot for it.
        locked_data = data.pop('__locked__', None)
        # And a nested item's PHI status, at every depth: only an
        # item writes it, but a hand-edited root key must not become a
        # tag either, nor a status (see the restore at the end).
        phi_data = data.pop('__phi__', None)

        # 1. Attributes
        target_item.attributes.update(data)
        if vrs_data:
            # Assigned, not recorded through `record_attr_vr`, for the
            # same reason the attributes above are: hydration restores a
            # state, it does not make an edit.
            target_item.attribute_vrs.update(vrs_data)
        if shifted_data:
            # Assigned rather than recorded through `record_date_shift`,
            # for the same reason: hydration restores a state.
            target_item._shifted_dates = dict(shifted_data)
        if remediated_data and hasattr(target_item, 'record_remediation'):
            # Assigned, not recorded, for the same reason.
            target_item._remediated_values = dict(remediated_data.get('values') or {}) or None
            target_item._remediated_blank = remediated_data.get('blank') or None
        if isinstance(locked_data, str) and locked_data \
                and hasattr(target_item, 'record_identity_token'):
            # Assigned, not recorded, for the same reason.
            target_item._locked_token = locked_data

        # 2. Sequences
        if sequences_data:
            from .entities import DicomItem
            for tag, items_list in sequences_data.items():
                # Before the item loop, and unconditional. `_serialize_item`
                # stores a zero-item sequence as `{"0009,1005": []}`, and
                # iterating an empty list calls `add_sequence_item` zero
                # times, so without this an empty `SQ` would not survive
                # a save/close/reopen.
                target_item.add_sequence(tag)
                for item_data in items_list:
                    new_item = DicomItem()
                    self._deserialize_into(new_item, item_data)
                    target_item.add_sequence_item(tag, new_item)

        # 3. A nested item's status, as this item's **last** step:
        # its own sub-sequences are built above, and each
        # `add_sequence_item` advances this item's revision, so a status
        # recorded earlier would already read stale. The parent's
        # `add_sequence_item` that follows advances only the parent, and
        # the load's `mark_subtree_persisted()` clears the change this
        # records. Never on an `Instance`: the root's status comes from its
        # column, and a hand-edited root key must not land a status there
        # even for the moment before the column's overwrites it. Only a
        # well-formed `{"status": <str>}`; anything else reads UNSCANNED
        # through `_phi_status_from_stored`, and nothing here raises. No
        # policy: an item's status never has one.
        if (not isinstance(target_item, Instance)
                and isinstance(phi_data, dict)
                and isinstance(phi_data.get('status'), str)):
            status = _phi_status_from_stored(phi_data['status'])
            if status is not PhiStatus.UNSCANNED:
                target_item.record_phi_status(status, policy=None)

    def save_vertical_attributes(
            self, instance_uid: str, attributes: Dict[Tuple[str, str], Any],
            conn: sqlite3.Connection = None,
            vrs: Dict[Tuple[str, str], str] = None):
        """Persists private tags to the vertical `instance_attributes` table.

        The write **replaces the instance's whole vertical set**: every row for
        `instance_uid` is deleted first, then the given attributes are inserted.
        An empty mapping therefore clears the instance, and is not a no-op: a
        tag removed from the graph must not stay in the table, or the next
        reload puts it back.

        One row per value atom. `value_text` is the atom's text, NULL for an
        atom whose value was `None`. `value_count` is the container's length
        on every row of an element, NULL for a scalar; an empty container
        writes one placeholder row (atom 0, `value_text` NULL,
        `value_count` 0).

        Args:
            instance_uid (str): The SOP Instance UID.
            attributes (Dict[Tuple[str, str], Any]): Mapping of (Group, Element) hex strings to values.
            conn (sqlite3.Connection, optional): An existing database connection to use for the transaction.
            vrs (Dict[Tuple[str, str], str], optional): The source VR for
                each tag, keyed the same way. A tag with no entry stores
                `"UN"`: its VR was never known.

        Raises:
            sqlite3.Error: The write failed; logged and re-raised, so the
                caller's transaction rolls back.
        """
        data_rows = []
        for (grp, elem), val in attributes.items():
            # `"UN"` is the default, and it is not a placeholder: it says
            # this value's VR was never recorded, which is what an Implicit
            # VR source produces for every private element.
            # `load_vertical_attributes_bulk` reads it back and
            # `_value_fits_vr` refuses it, so such a value takes the
            # export fallback.
            vr = (vrs or {}).get((grp, elem), "UN")
            # Check for VM > 1. `MultiValue` is what pydicom hands back for a
            # multi-valued element and it is a MutableSequence, NOT a list, so
            # a bare `isinstance(val, list)` would send it down the scalar arm
            # and store "['a', 'b', 'c']" in one row -- a string that reloads
            # looking like a list. `IsocenterJSONEncoder` unwraps MultiValue
            # for the other tier for the same reason. `tuple` is here
            # because `_merge` names it: a `()` on the scalar arm would be
            # stored as the text `'()'` and reload as a value the source
            # never had.
            if isinstance(val, (list, tuple, MultiValue)):
                if not val:
                    # The placeholder row for an empty container.
                    # There is no atom, so `value_text` is NULL and the
                    # read side throws the atom away without looking at
                    # it -- the row exists only to carry the `0`. A
                    # container with no values still has to write
                    # something, or "the tag was present and empty" and
                    # "the tag was not there" would be the same absence
                    # of rows.
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
            # Re-raised, never swallowed. The DELETE above is
            # de-identification work: it is what removes a private tag the
            # graph no longer has. Swallowing the error here would let the
            # caller's transaction commit the instance row and mark the
            # instance persisted, so a save that failed to strip the vendor
            # block would report success and never be retried. The raise
            # reaches `save_all`, which rolls back and leaves the instances
            # dirty.
            self.logger.error(f"Failed to save vertical attributes for {instance_uid}: {describe_exception(e)}")
            raise

    def load_vertical_attributes(self, instance_uid: str) -> Dict[Tuple[str, str], Any]:
        """Loads one instance's private tags from the vertical table.

        See `load_vertical_attributes_bulk` for how values come back.

        Args:
            instance_uid (str): The SOP Instance UID.

        Returns:
            Dict[Tuple[str, str], Any]: (group, element) -> value; empty if
                the instance has no rows.
        """
        return self.load_vertical_attributes_bulk([instance_uid]).get(instance_uid, {})

    def reconcile_private_tags(self) -> Tuple[int, Dict[str, List[str]]]:
        """Drop `instance_attributes` rows absent from the core attributes.

        The database half of `DicomSession.reconcile_private_tags()`, which
        carries the public contract and the warnings; use that. This method
        applies one rule: the core `attributes_json` is read as the complete
        answer to "which tags does this instance have", and every
        `instance_attributes` row whose tag is not there is deleted. For a store
        that keeps its private tags, those rows are the private data, which is
        why nothing calls this automatically.

        A row whose instance is not in `instances` at all is dropped too: it
        can reach no graph from this store.

        Returns:
            Tuple[int, Dict[str, List[str]]]: rows deleted (rows, not
                tags: a value with multiplicity 3 is three rows), and
                per-instance `{sop_instance_uid: [tags]}` so the caller can heal
                the live graph and write the audit trail.
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

        One query, stitched in memory, so loading many instances does not mean
        one query per instance.

        Values come back as they are stored (`str`, or a `list` of `str` for
        VM > 1) **except** where `value_rep` names a VR whose values are not text
        on the wire: `US`, `UL`, `FL` and their siblings come back as numbers,
        because pydicom refuses a `str` for them at write time
        (`_VERTICAL_VR_PARSERS`). Nothing is inferred from the text: the VR the
        source file gave decides, and a tag stored under `"UN"` (every private
        element of an Implicit VR source) reloads as text.

        An element's VR and count are read off its first row. A count of `0`
        over the placeholder row is an empty list; `n >= 1` is a list even at
        `n == 1`; NULL means more than one atom is a list and one is a scalar.
        The count never truncates or pads: the stored atoms are the values.
        An atom stored as a NULL `value_text` comes back as `None`.

        Args:
            instance_uids (Optional[List[str]]): SOP Instance UIDs to fetch.
                `None` means every row in the table, which is what a
                whole-store load wants. A list is chunked to stay under
                SQLite's bound-parameter limit.
            conn (sqlite3.Connection, optional): An existing connection to
                read on. Callers already inside a `_get_connection` block
                MUST pass theirs: on a `:memory:` store a nested connection
                deadlocks.
            vrs (Optional[Dict]): An accumulator, filled with SOP Instance
                UID -> {(group, element): value_rep} when given. `"UN"`
                entries are included: "no VR was recorded" is an answer.

        Returns:
            Dict[str, Dict[Tuple[str, str], Any]]: SOP Instance UID ->
                {(group, element): value}. Instances with no vertical rows are
                absent rather than present-and-empty.

        Raises:
            sqlite3.Error: The read failed. Not swallowed: an empty result
                would be indistinguishable from an instance with no private
                tags.
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

        # No `except sqlite3.Error: return {}` here. An empty result from
        # this method is indistinguishable from an instance that genuinely
        # has no private tags, so swallowing a read failure would leave
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
            # atom both carry a NULL `value_text` and differ only here, so
            # a reader that looked at the text first would reload `[]` as
            # `[None]`.
            #
            # `values == [None]` is the placeholder row's exact shape --
            # one atom, NULL text -- and no writer produces `0` over any
            # other. Checking it keeps the no-truncation rule without an
            # exception: a hand-edited store carrying `0` over three real
            # atoms hands back the three, where a bare `count == 0` would
            # return `[]` and drop values that are sitting in the table.
            if count == 0 and values == [None]:
                value = []
            elif count is None:
                # No recorded arity: more than one atom is a list, one is a
                # scalar -- what a scalar wants and the only honest answer
                # for a row written before the column existed.
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

        Assigns directly, so the instance's revision does not move. VRs of
        `"UN"` are not recorded.

        Args:
            instance (Instance): The instance being hydrated.
            private (Dict[Tuple[str, str], Any]): (group, element) -> value.
            vrs (Dict[Tuple[str, str], str], optional): (group, element) ->
                stored VR.
        """
        # Into `attributes` directly, exactly as `_deserialize_into` does,
        # never through `set_attr`: `set_attr` advances `_revision`, and a
        # status recorded against a revision the entity has since left
        # reads back as UNSCANNED, so an instance rebuilt from a row that
        # recorded a conclusion would report that nothing is known about
        # it. Both callers also apply these before their status restore and
        # `mark_subtree_persisted()`, which would absorb a stray bump and
        # hide it from a round trip; keep that order as defence, but direct
        # assignment is the rule.
        for (grp, elem), value in private.items():
            instance.attributes[f"{grp},{elem}"] = value

        # Same rule for the VR carrier, and for the same reason:
        # assigned, never recorded through `record_attr_vr`. `"UN"` is
        # skipped rather than stored -- it means no VR was ever known,
        # and a carrier entry saying "unknown" is not the same thing as
        # no entry, which is what `_merge` reads.
        for (grp, elem), vr in (vrs or {}).items():
            if vr and vr != "UN":
                instance.attribute_vrs[f"{grp},{elem}"] = vr

    def persist_blob(self, instance, kind: str, data) -> None:
        """Write a binary blob to the sidecar and record its reference.

        Appends the payload zlib-compressed, records it in `instance_blobs`
        under the sidecar gate, and marks the instance modified. `data` of
        None writes nothing.

        Args:
            instance (Instance): Owning instance.
            kind (str): A blob kind: `'pixels'`, `'waveform'`, or either
                followed by a sequence path. See `parse_blob_kind`.
            data (bytes | np.ndarray): Payload. Arrays are passed to the
                sidecar directly to avoid a full copy.

        Raises:
            ValueError: If `kind` does not match the blob-kind grammar. The
                message carries the grammar; `serialize_blob_kind` spells a
                valid kind.
            RuntimeError: The sidecar gate was not acquired within
                `_SIDECAR_GATE_TIMEOUT_S`.
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
        # the pre-compaction layout of a smaller file.
        with self._hold_sidecar_gate():
            offset, length = self.sidecar.write_frame(data, c_alg)
            self.record_blob_ref(
                instance.sop_instance_uid, kind, offset, length, digest, c_alg)

        instance.mark_modified()

    def record_blob_ref(self, instance_uid: str, kind: str, offset: int,
                        length: int, blob_hash: str, compress_alg: str,
                        conn: sqlite3.Connection = None) -> None:
        """Record a sidecar reference without writing to the sidecar.

        For a frame written directly through `SidecarManager` (the ingest
        path): an unrecorded frame is invisible to `compact_sidecar` and is
        reclaimed as dead space. Upserts on `(instance_uid, kind)`; a None
        `blob_hash` keeps the hash already recorded.

        Callers already inside a transaction MUST pass their connection:
        a nested one blocks for the full `_SQLITE_BUSY_TIMEOUT_S` and then
        fails on a file-backed store, and deadlocks on a `:memory:` one.

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
        # Both doors, one answer: `persist_blob` validates too. This is the
        # door the *ingest* path uses, because it writes its own frames
        # through `SidecarManager` and registers the reference separately.
        # A gate on one of two doors is not a gate.
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

        After a compaction, this is where non-pixel loaders read their new
        offsets: `compact_sidecar`'s map covers top-level pixel blobs only.

        Args:
            kind (str): 'pixels' or 'waveform'.

        Returns:
            Dict[str, Tuple[int, int]]: instance_uid -> (offset, length), for
                rows that have both.
        """
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT instance_uid, offset, length
                FROM instance_blobs
                WHERE kind = ? AND offset IS NOT NULL AND length IS NOT NULL
            """, (kind,)).fetchall()

        return {r["instance_uid"]: (r["offset"], r["length"]) for r in rows}

    def get_nested_pixel_refs(self) -> Dict[Tuple[str, str], Tuple[int, int]]:
        """Every nested pixel reference, keyed `(instance_uid, kind)`.

        One instance can carry a top-level `pixels` blob *and* one row per
        nested item, so the key includes the kind. Read after a compaction
        to rewire nested references to the new offsets.

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
        """Write the resident pixel array to the sidecar, so it can be unloaded.

        Appends the array (zlib) under the sidecar gate, rebinds the
        instance's `_pixel_loader` and `_pixel_hash` to the new frame,
        records it in `instance_blobs` and marks the instance modified. The
        `instances` row is not written; the next save does that. An
        instance with no resident array is left alone.

        Args:
            instance (Instance): The instance whose pixel data to persist.

        Raises:
            Exception: Any failure, including a sidecar gate timeout
                (`RuntimeError`), is logged and re-raised.
        """
        try:
            # Site 5 of six. The gate is taken OUTSIDE `_pixel_swap_lock`
            # (order: gate -> swap lock, the order `_rewire_sidecar_loaders`
            # needs) and spans the append below AND the `record_blob_ref`
            # after the swap lock is released. Inside this `try`
            # so a gate expiry gets the same log line as any other swap
            # failure before it becomes a `RedactionOutcome(ok=False)`.
            with self._hold_sidecar_gate():
                self._swap_pixels_under_gate(instance)
        except Exception as e:
            self.logger.error(f"Failed to persist pixel swap for {instance.sop_instance_uid}: {describe_exception(e)}")
            raise

    def _swap_pixels_under_gate(self, instance: Instance):
        """`persist_pixel_data`'s body; the caller holds the sidecar gate.

        Args:
            instance (Instance): The instance whose pixel data to persist.
        """
        # The read -> sidecar write -> loader/hash rebind must be one
        # critical section against `_persist_pixels`: a background
        # save that reads the bytes before this redaction swap zeroes
        # them, and rebinds after it, leaves the instance reading
        # back its pre-redaction pixels under a redaction attestation.
        # Released before `record_blob_ref` below -- never
        # hold a thread lock across a sqlite write that can wait out
        # the busy timeout. (The gate, held by the caller, is the
        # one deliberate exception to that rule, and the reason is
        # at site 4.)
        with self._pixel_swap_lock:
            # 1. Write to Sidecar
            # Pass array directly to avoid a .tobytes() memory spike.
            # One read, inside the lock, and every branch below asks this
            # local: a None check outside the lock would let an
            # `unload_pixel_data()` land between it and the read, and
            # `hashlib.sha256(None)` would raise into the redaction swap.
            # Returning here skips `record_blob_ref` and `mark_modified`.
            b_data = instance.pixel_array
            if b_data is None:
                return
            # The revision beside the read, for the publish below to check
            # with the identity. `set_pixel_data()` keeps a native-order
            # array as given, so a caller can edit the resident array in
            # place and set the same object again: landing after the
            # write, that set passes an identity check alone while the
            # sidecar holds the bytes from before the edit, and the flag
            # cleared over them lets an unload drop the only copy. Read
            # without the leaf: a set caught
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
            # raised (a full disk, an EIO) would leave the instance holding
            # the hash of a frame that was never written beside a loader
            # still on the original; the next save's `arr is None` arm
            # would store that hash with the original's offset, and a
            # reopened session, which checks it, would refuse a correct
            # frame as a hash mismatch.

            # The swap always writes zlib.
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
            # before or after this publish. The loader is rebound either
            # way: this is the redaction swap, and the instance must not
            # be left on the pre-redaction frame.
            with entities.PIXEL_STATE_LOCK:
                instance._pixel_loader = swapped
                instance._pixel_hash = p_hash
                # Only if the array written is still the one resident,
                # at the revision it was read at. A set that landed since
                # holds newer, unwritten pixels -- a new array, or this
                # one edited in place and set again -- and clearing the
                # flag would let an unload drop them; a discard since
                # already cleared the record.
                if (instance.pixel_array is b_data
                        and instance._revision == read_revision):
                    # The loader now points at the bytes that are
                    # resident, so the array is recoverable and freeable
                    # again.
                    instance._pixel_array_unwritten = False
                    # And they are the stored frame now, which the
                    # current descriptors describe: nothing is left for a
                    # discard to undo.
                    instance._pixel_descriptors_replaced = None

        # 3. The `instances` row is not written here: the in-memory
        # loader (step 2) is what `unload_pixel_data()` needs, and the
        # next session.save() records the new offset/length there.

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
        """Incrementally persist the given patients and their graph.

        Walks Patient -> Study -> Series -> Instance, upserting anything
        that has unsaved changes (pixel frames are appended to the sidecar
        first), and deleting rows for studies, series and instances the
        saved objects no longer hold. The rows are written in one
        transaction, so a failure leaves the database as it was found (a
        frame already appended stays in the sidecar as reclaimable dead
        space). Instances are marked persisted, at the revision captured
        before their write, only after the commit returns. Holds the
        sidecar gate for the whole save. Logs one WARNING when two
        `Patient` objects share an ID.

        Args:
            patients (List[Patient]): The patient objects to save.
            prune_absent_patients (bool): Delete patient rows that `patients`
                does not contain. Only correct when the list is the entire
                contents of the session, so it defaults to off: a partial
                save that pruned would turn "store this one patient" into
                "delete everyone else". `DicomSession.save()` owns the whole
                store and passes True, which is what stops an anonymised
                patient's original row surviving under its old identifier.

        Raises:
            RuntimeError: The sidecar gate was not acquired within
                `_SIDECAR_GATE_TIMEOUT_S`.
            Exception: Any failure inside the transaction, logged and
                re-raised after the rollback; nothing is marked persisted.
        """
        self.logger.info(
            "Saving %d patients to %s (Incremental)...", len(patients), self.db_path)

        tally = _SaveTally()
        _warn_on_shared_patient_ids(self.logger, patients)
        # Instances are marked clean only after the commit returns. Doing it
        # inside the walk would let a rolled-back save leave memory
        # believing it was written, so the retry would skip exactly the
        # rows that failed. These are references to objects that
        # are already resident, so holding them costs a pointer each.
        saved_instances = []

        # Site 6 of six. The gate spans `_prepare_pixel_frames` AND the
        # transaction's commit -- the whole of what follows up to the
        # `mark_persisted` loop. A gate released between the prepass and
        # the commit lets a row commit after `_apply_new_offsets` for a
        # frame appended before `_read_blob_index`. The prepass takes
        # `_pixel_swap_lock` per instance under this gate, which is the
        # order the rewire needs. `compact()` takes this same gate only
        # after its leading `save(sync=True)` has returned, or it would
        # deadlock here against itself.
        with self._hold_sidecar_gate():
            # Every sidecar frame this save will need is appended HERE,
            # before any connection exists. Inside the walk below, the
            # SQLite write lock would be held for as long as the save's
            # whole dirty resident pixel payload takes to compress and
            # write -- so a slow-storage save could outlast
            # `_SQLITE_BUSY_TIMEOUT_S` and surface in a healthy concurrent
            # writer as `database is locked`. The transaction below
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

                    # Every upsert in the whole save has now run, so a
                    # child that moved to a different parent already
                    # points at it and a scoped delete will not mistake
                    # it for a removal.
                    #
                    # And no scoped delete removes a row some object in
                    # `patients` still holds. Two parent objects
                    # can share one row -- two `Patient`s with one
                    # `patient_id` resolve to one `patients` row -- and a
                    # delete scoped to one object's list then removes the
                    # children the other object holds. Built HERE, inside
                    # the transaction and after the walk, not beside the
                    # prepass: redaction renames `sop_instance_uid` in
                    # place under no lock this save takes, and a set
                    # frozen at the prepass would keep the old UID's row
                    # beside the renamed one's.
                    held = _held_uids(patients)
                    for delete, parent, parent_pk in pending_deletions:
                        delete(cur, parent, parent_pk, held)

                    if prune_absent_patients:
                        self._delete_absent_patients(cur, patients)

                    conn.commit()
            except Exception:
                # No rollback here: `_get_connection` owns the transaction
                # and has already rolled it back and closed the connection
                # by the time this runs. `conn.rollback()` on the closed
                # handle would raise `ProgrammingError: Cannot operate on
                # a closed database` and replace the real exception.
                self.logger.error(
                    "Save failed; the transaction was rolled back and "
                    "nothing was marked clean", exc_info=True)
                raise

        for instance, revision in saved_instances:
            instance.mark_persisted(revision)

        self._log_save_summary(tally)

    def _save_patient(self, conn, cur, patient, tally,
                      pending_deletions, prepared) -> List[Tuple[Instance, int]]:
        """Persists one patient's subtree.

        Deletions are appended to `pending_deletions`, not run; the caller
        runs them after every patient has been walked.

        Args:
            conn: The save's open connection.
            cur: A cursor on it.
            patient (Patient): The patient to write.
            tally (_SaveTally): Counts what is written.
            pending_deletions (list): Receives `(delete, parent,
                parent_pk)` entries.
            prepared: `_prepare_pixel_frames`'s result.

        Returns:
            List[Tuple[Instance, int]]: The instances written, each with
                the revision to mark persisted; empty if the patient has no row.
        """
        # A scoped `WHERE parent_id_fk = ?` delete is only correct once
        # the row it might delete has had the chance to be claimed by its
        # new parent, so deletions wait until every parent is walked.
        patient_pk = self._upsert_patient(cur, patient, tally)
        if patient_pk is None:
            return []

        # The study-level sibling of the two re-parentings below, and the
        # one that matters most: a patient's ID is exactly what
        # de-identification replaces, so after a reload every Patient ID
        # replacement writes a NEW patient row, and a study the pass did
        # not change is never upserted onto it. Its key would go on naming
        # the old row, which `_delete_absent_patients` deletes with the
        # study beneath it. A merge that moves a clean study to the
        # patient already holding its new ID depends on this too.
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
            # what makes the deferred deletion below see the truth.
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

        A patient with no row under its ID is written whatever its revision
        says. `jitter_scheme` is set on insert and never overwritten.

        Args:
            cur: A cursor in the save's transaction.
            patient (Patient): The patient.
            tally (_SaveTally): Counts a write.

        Returns:
            Optional[int]: The row's primary key, or None if no row exists
                after the write.
        """
        # "Or missing": a write that goes around the tracked-field path can
        # leave a clean patient under an ID with no row; without this the
        # save would write nothing, find no key, skip the whole subtree,
        # and the prune would delete the old ID's row with every study
        # beneath it. It reads the store rather than the bookkeeping, so it
        # moves no revision and needs no setter.
        existing = cur.execute(
            "SELECT id FROM patients WHERE patient_id=?",
            (patient.patient_id,)).fetchone()
        if patient.has_unsaved_changes or existing is None:
            # `jitter_scheme` is written on every INSERT, so a NULL can
            # only come from a build that predates the column, and never
            # overwritten on conflict: a patient's class is fixed once,
            # and a save must not reclassify it.
            cur.execute("""
                INSERT INTO patients (patient_id, patient_name, phi_status,
                                      phi_policy, phi_policy_base,
                                      phi_status_edited, jitter_scheme)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(patient_id) DO UPDATE SET
                    patient_name=excluded.patient_name,
                    phi_status=excluded.phi_status,
                    phi_policy=excluded.phi_policy,
                    phi_policy_base=excluded.phi_policy_base,
                    phi_status_edited=excluded.phi_status_edited,
                    jitter_scheme=COALESCE(patients.jitter_scheme,
                                           excluded.jitter_scheme)
            """, (patient.patient_id, patient.patient_name,
                  *_status_columns(patient), patient._jitter_scheme))
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
                                     shifted_study_date, phi_status,
                                     phi_policy, phi_policy_base,
                                     phi_status_edited)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(study_instance_uid) DO UPDATE SET
                    study_date=excluded.study_date,
                    date_shifted=excluded.date_shifted,
                    shifted_study_date=excluded.shifted_study_date,
                    patient_id_fk=excluded.patient_id_fk,
                    phi_status=excluded.phi_status,
                    phi_policy=excluded.phi_policy,
                    phi_policy_base=excluded.phi_policy_base,
                    phi_status_edited=excluded.phi_status_edited
            """, (patient_pk, study.study_instance_uid,
                  _as_stored_date(study.study_date),
                  1 if study.date_shifted else 0,
                  # Plain assignment, not COALESCE-guarded, for
                  # `shift_provenance`'s reason: a study whose
                  # record is None has to be able to write that None, or
                  # a record could never be cleared and a stale one
                  # would go on vouching for a value the graph no longer
                  # holds.
                  study._shifted_study_date,
                  *_status_columns(study)))
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

        Run for every series, changed or not. A partial save
        (`prune_absent_patients=False`, a sub-list) still deletes a row
        whose object moved to a patient outside the list: `held` is built
        from the list given, not the whole session.

        Args:
            cur: A cursor in the save's transaction.
            series (Series): Not read; the deletion entries share one shape.
            series_pk (int): The series row whose instances are checked.
            held (_HeldUids): Every UID the saved list holds.

        Returns:
            int: How many instance rows were deleted.
        """
        # Removing an instance from a series' list mutates a plain list,
        # which marks nothing, so comparing the sets is the only way to
        # notice. Compared against `held` -- every UID the saved list
        # holds, at any parent -- not this series' own list: two `Series`
        # objects can share this row, and each list is only half of what
        # memory holds.
        stored = {row[0] for row in cur.execute(
            "SELECT sop_instance_uid FROM instances WHERE series_id_fk=?",
            (series_pk,)).fetchall()}
        removed = stored - held.instances
        _delete_instances(cur, removed)
        return len(removed)

    @staticmethod
    def _delete_removed_series(cur, study, study_pk, held) -> int:
        """Deletes this study's series that no object in the save holds, and
        their instances. `held` is as in `_delete_removed_instances`, whose
        body comment gives the reason for comparing against it."""
        stored = {row[1]: row[0] for row in cur.execute(
            "SELECT id, series_instance_uid FROM series WHERE study_id_fk=?",
            (study_pk,)).fetchall()}
        removed = [pk for uid, pk in stored.items() if uid not in held.series]
        _delete_series_subtrees(cur, removed)
        return len(removed)

    @staticmethod
    def _delete_removed_studies(cur, patient, patient_pk, held) -> int:
        """Deletes this patient's studies that no object in the save holds,
        and their subtrees. `held` is as in `_delete_removed_instances`,
        whose body comment gives the reason for comparing against it."""
        stored = {row[1]: row[0] for row in cur.execute(
            "SELECT id, study_instance_uid FROM studies WHERE patient_id_fk=?",
            (patient_pk,)).fetchall()}
        removed = [pk for uid, pk in stored.items() if uid not in held.studies]
        _delete_study_subtrees(cur, removed)
        return len(removed)

    @staticmethod
    def _delete_absent_patients(cur, patients) -> int:
        """Deletes patient rows the in-memory store no longer contains.

        Patients are upserted on `patient_id`, so replacing an ID writes a
        *new* row and leaves the old one, with the original name and
        identifier still in it; this deletes it, with everything still
        beneath it. Call only after every patient has been written: the
        studies must already be re-parented to the new rows, or this
        deletes the subtree a new row is about to adopt.

        Args:
            cur: A cursor in the save's transaction.
            patients (List[Patient]): Every patient the store holds.

        Returns:
            int: How many patient rows were deleted.
        """
        # `_reparent_studies` points every study the patient holds at the
        # new row on every save, dirty or not, so no scoped deletion ever
        # reaches the old row; only this does.
        stored = {row[1]: row[0] for row in cur.execute(
            "SELECT id, patient_id FROM patients").fetchall()}
        in_memory = {p.patient_id for p in patients}
        removed = [pk for pid, pk in stored.items() if pid not in in_memory]
        _delete_patient_subtrees(cur, removed)
        return len(removed)

    def _save_unsaved_instances(self, conn, cur, series, series_pk,
                              tally, prepared) -> List[Tuple[Instance, int]]:
        """Upserts the instances of one series that the prepass claimed.

        The dirty set and each revision were fixed by
        `_prepare_pixel_frames`: an instance dirtied afterwards is not
        saved this round and stays dirty for the next. Also writes each
        instance's `instance_blobs` rows and its vertical private tags.

        Args:
            conn: The save's open connection.
            cur: A cursor on it.
            series (Series): The series whose instances are written.
            series_pk (int): Its row's primary key.
            tally (_SaveTally): Counts what is written.
            prepared: `_prepare_pixel_frames`'s result.

        Returns:
            List[Tuple[Instance, int]]: The (instance, revision) pairs
                written, for the caller to mark persisted once the transaction
                has committed. The revision was captured *before* the write, so
                an edit arriving mid-save is not mistaken for the stored state.
        """
        # Selected by object identity, never by SOP Instance UID. A UID can
        # change in place between the prepass and here (redaction does
        # exactly that), and a UID-keyed miss does not merely skip the
        # write: `_delete_removed_instances` then sees the instance's OLD
        # row as orphaned and deletes it, so the instance vanishes from the
        # index. The rows read the LIVE `inst.sop_instance_uid`, so a
        # renamed instance is inserted under its new name and its old row
        # is reaped as removed.
        unsaved = [(inst, prepared[inst][0])
                   for inst in series.instances
                   if inst in prepared]
        if not unsaved:
            return []

        rows, blob_rows, vertical_rows = self._build_instance_writes(
            unsaved, series_pk, tally, prepared)

        cur.executemany(_UPSERT_INSTANCE_SQL, rows)

        # Routed through record_blob_ref rather than inlined, so that exactly
        # one place knows how an instance_blobs row is written; a
        # duplicated SQL statement's conflict clause can drift from its
        # sibling. `conn=conn` keeps it in this transaction.
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

        Does no I/O: the frames are already written, in `prepared`, and
        the caller decides when these rows go in. An instance whose
        revision has moved since the capture gets an all-None frame, so its
        stored pixel reference is left alone and it stays dirty.

        Args:
            unsaved: `(instance, revision captured by the prepass)` pairs.
            series_pk (int): The owning series row.
            tally (_SaveTally): Not read here.
            prepared: `_prepare_pixel_frames`'s result.

        Returns:
            Tuple of (instance rows, instance_blobs rows, (uid, private
                attributes, private VRs) triples for the vertical table).
        """
        # No I/O of any kind, and there must not be: this runs inside
        # `save_all`'s open transaction. The captured revision is the one
        # `mark_persisted` will be given, so the revision guard is
        # re-evaluated below, at the last moment, against a window as long
        # as all of the save's pixel I/O.
        rows, blob_rows, vertical_rows = [], [], []

        for inst, revision in unsaved:
            core, private = _split_core_and_private(self._serialize_item(inst))
            # The VRs for exactly the tags that made it to the private
            # tier, re-keyed to that tier's `("gggg", "eeee")` shape. Only
            # those: an entry for a tag whose value stayed in
            # `attributes_json` would be a VR with no row to sit on.
            private_vrs = {}
            for key in private:
                recorded = inst.attribute_vrs.get(",".join(key))
                if recorded:
                    private_vrs[key] = recorded
            # Appended even when `private` is empty. An instance whose
            # private tags were all stripped still has to reach
            # `save_vertical_attributes`, or its old rows stay in the table
            # and the next reload puts them back on the graph.
            vertical_rows.append((inst.sop_instance_uid, private, private_vrs))

            # The revision guard, re-checked at the latest possible moment.
            # `_persist_pixels` applied it when it appended the frame, but
            # the prepass runs before the transaction opens, so the
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
            # stale capture. The instance's row is still written, NOT
            # dropped.
            frame = prepared[inst][1]
            if inst._revision != revision:
                frame = _StoredFrame(None, None, None, None)
            status_columns = _status_columns(inst)
            rows.append((
                series_pk, inst.sop_instance_uid, inst.sop_class_uid,
                # Positional, and nothing checks this tuple against
                # `_UPSERT_INSTANCE_SQL`'s column list: `source_path`
                # sits immediately after `file_path` in both, and an
                # insertion in one and not the other writes the sidecar
                # offset into the path column without raising.
                inst.instance_number, inst.file_path, inst.source_path,
                frame.offset, frame.length, frame.hash, frame.alg,
                json.dumps(core, cls=IsocenterJSONEncoder),
                # UNSCANNED for an entity edited since the scan, as the
                # property reads -- the row's attributes are the edited
                # ones -- with the status the edit left in the last column
                # (`phi_status_edited`). Its policy comes from the same
                # read, after `shift_provenance` as the columns are.
                status_columns[0],
                # NULL keeps a legacy instance legacy for its life in the
                # store; anything this code created or ingested says so.
                None if inst._legacy_shift_provenance else 'recorded',
                *status_columns[1:]))

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

            # Nested payloads join the same batch. Two things about this
            # loop that a per-blob `persist_blob` would get wrong.
            #
            # It re-emits the **row** every save, keyed on the instance's
            # *current* `sop_instance_uid` -- which is what makes the
            # reference follow a `regenerate_uid()`. `instance_blobs` is
            # keyed by UID, redaction changes it, and a row left under the
            # retired UID is an orphan only `compact()` notices.
            #
            # And it never re-appends the **frame**. Nothing in the pipeline
            # mutates an icon: remediation edits `attributes`, redaction
            # touches the top-level array, anonymize touches neither. The
            # bytes were written once at ingest and the ref still points at
            # them.
            #
            # Batched rather than one `persist_blob` per payload, which
            # opens its own connection each and is far slower. Follow
            # `save_all`'s shape.
            for (path, terminal_tag), ref in inst._nested_pixel_refs.items():
                blob_rows.append((
                    inst.sop_instance_uid,
                    serialize_blob_kind('pixels', path, terminal_tag),
                    ref.offset, ref.length, ref.blob_hash, ref.alg))

            # The waveform's row, re-emitted for the same reason as the
            # nested rows above: ingest writes it once, under the UID of
            # that moment, and it has no column on `instances` to ride.
            # `anonymize()` replaces the SOP Instance UID, and a row left
            # under the source UID would reopen the instance with no
            # samples and be reclaimed by `compact()` as an orphan's.
            # The loader holds the frame's reference; never re-append.
            wave = inst._waveform_loader
            if getattr(wave, "offset", None) is not None and wave.length is not None:
                blob_rows.append((
                    inst.sop_instance_uid, 'waveform', wave.offset,
                    wave.length, inst._waveform_hash, wave.alg))

        return rows, blob_rows, vertical_rows

    def _prepare_pixel_frames(
            self, patients, tally) -> Dict[Instance, Tuple[int, '_StoredFrame']]:
        """Appends every dirty instance's pixel frame, before any transaction.

        Walks Patient -> Study -> Series -> Instance once and, for each
        instance with unsaved changes, captures its revision and calls
        `_persist_pixels`. The dirty set is frozen here: an instance
        dirtied after this walk is not saved this round. A frame appended
        here whose transaction then rolls back (or that the revision guard
        leaves unpublished) is referenced by nothing and stays in the
        sidecar as dead space, bounded by one save's dirty resident pixel
        bytes, until a manual `session.compact()`.

        Args:
            patients (List[Patient]): The patients being saved.
            tally (_SaveTally): Counts frames and bytes appended.

        Returns:
            Dict[Instance, Tuple[int, _StoredFrame]]: instance ->
                (revision captured before the write, the frame written).
        """
        # Runs ahead of `save_all`'s connection so the transaction does row
        # upserts and nothing else -- no compression, no sidecar append, no
        # `flock` wait -- and the SQLite write lock is not held for the
        # length of the save's pixel payload. The earlier capture is the
        # safe direction: `mark_persisted` receives an older revision, so
        # anything changing in the window leaves the instance dirty.
        #
        # Keyed by **object identity**, the only key that survives what can
        # happen before the transaction. Position cannot: `series_pk` is
        # unknowable before it and a series can be re-parented in between.
        # The SOP Instance UID cannot either: redaction mutates
        # `sop_instance_uid` **in place** (`regenerate_uid()`,
        # `Session._apply_redaction_outcomes`), so a UID-keyed lookup misses
        # a renamed instance, which the walk then skips while
        # `_delete_removed_instances` deletes its old row as orphaned.
        # Entities hash and compare by identity (`eq=False` on every
        # `TrackedEntity` subclass), so a renamed instance is still found.
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

        Resident pixels are hashed and appended only if the bytes changed
        (identical bytes rebuild the loader in place); pixels already
        swapped out keep the frame the loader holds; no pixels gives the
        all-None frame. On a write it rebinds `_pixel_loader` and
        `_pixel_hash`. The caller holds the sidecar gate; this takes
        `_pixel_swap_lock`.

        Args:
            inst (Instance): The instance.
            tally (_SaveTally): Counts frames and bytes appended.
            revision (int, optional): The revision captured before the save
                started. If the instance has moved past it, nothing is
                published and the all-None frame comes back, so the upsert
                leaves the stored reference alone and the instance stays
                dirty.

        Returns:
            _StoredFrame: What the instance row should record.
        """
        # This runs on the persistence manager's thread against live
        # objects a redaction pass may be mutating, so publishing what is
        # read here needs two protections: the lock makes read -> write ->
        # rebind atomic against `persist_pixel_data`, so this save's rebind
        # can never land *after* a redaction's and rewire the instance to
        # the stale frame; and the revision guard is checked at assignment
        # time inside the lock (outside it, the check would be decorative).
        #
        # No sqlite work happens in here; the caller writes the rows
        # later, off this lock. `write_frame` takes an `fcntl.flock` on
        # the file, which is cross-process and a leaf. The caller --
        # `save_all`, site 6 -- holds the sidecar gate around the whole
        # prepass and transaction, so the order at this `write_frame` is
        # gate -> `_pixel_swap_lock` -> the leaf flock. Do not take the
        # gate in here: it would be taken under the swap lock, the
        # reverse of the order the rewire needs.
        #
        # The lock must open *before* the first read of `pixel_array`,
        # not after it. `Instance.unload_pixel_data()` nulls that field
        # under no lock at all -- from `release_memory()` sweeps and from
        # redaction paths' `finally` -- so asking "is it None?" outside
        # and re-reading it inside is a TOCTOU window: a null landing
        # between the two makes `.tobytes()` raise inside `save_all`'s
        # open transaction and roll the entire save back. One read, one
        # local, every branch below asks the local. The local is also
        # what makes the write correct rather than merely non-crashing:
        # unload drops only the instance's reference, numpy keeps the
        # buffer alive for ours, and unload never mutates contents -- so
        # bytes written from `arr` are still the instance's true pixels.
        # Every instance takes this lock, non-resident ones included; a
        # few hundred nanoseconds each, uncontended.
        with self._pixel_swap_lock:
            arr = inst.pixel_array
            loader = inst._pixel_loader

            if arr is None:
                # Recording the loader's own frame is correct here BECAUSE
                # the precondition is enforced upstream:
                # `unload_pixel_data()` refuses to null a *diverged* array
                # (one `set_pixel_data()` replaced after a save). Without
                # that refusal this arm would re-record the superseded
                # frame's offset/length/hash and the save would mark the
                # instance persisted -- store, sidecar, memory and
                # `_pixel_hash` all agreeing on the wrong frame, with every
                # integrity check passing.
                #
                # That does NOT make `arr is None` mean "the array equalled
                # the loader's frame", and it must not be read that way:
                # `discard_pixel_data()` nulls a diverged array on purpose
                # and leaves the divergence flag set. Nothing else nulls
                # `pixel_array` -- every other write to it
                # (`set_pixel_data()` and `get_pixel_data()`'s three arms)
                # fills it. So `arr is None` here means one of exactly three
                # things: `unload_pixel_data()` cleared an array that was
                # equal to what the loader points at, or a caller
                # deliberately discarded one -- the redaction `finally`
                # blocks, where reverting to the loader's frame IS the
                # intended outcome, and the discard also puts back the
                # descriptors that frame was stored with, so the row this
                # arm records describes it -- or
                # `Session._apply_redaction_outcomes` nulled it in the
                # same breath as rebinding the loader to the frame the
                # worker just redacted, which is the same intended outcome
                # reached from the parent side of a processes pass.
                # Recording the loader's frame is correct under all three.
                # Do not relax that refusal, or add a fourth nulling site,
                # without revisiting this arm.
                #
                # The loader recorded here may carry a *stale capture*: a
                # descriptor written with the pixels unloaded never passes
                # through a rebuild, so its Rows, BitsAllocated or
                # PixelRepresentation can describe an instance that no
                # longer exists. That is deliberately not repaired here.
                # This arm hands back only the offset, length, algorithm
                # and hash, none of which a descriptor edit changes, and
                # the descriptors reach the store from `attributes`. The
                # staleness is in how the bytes are *read*, the same before
                # and after a save, so the check lives on the read:
                # `Instance.get_pixel_data` compares the capture with the
                # instance on every read (`SidecarPixelLoader.describes`).
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
                # the bytes bit-identical and hits this dedup. Without a
                # rebuild the loader would rebuild the frame under the
                # superseded dtype, and because the export picks its pixel
                # container from `arr.dtype.kind`
                # (`_export_instance_worker`'s `arr.dtype.kind == 'f'`
                # test) rather than from `attributes`, a float instance
                # whose pixels were replaced with integers would export as
                # `FloatPixelData` beside an audit row reading `wrote 1 of
                # 1`.
                #
                # Rebuilt, not patched: the same window is open on every
                # field the snapshot holds, geometry included -- a
                # replacement of the same byte length at a new
                # Rows/Columns would reload at the old shape. One rebuild
                # answers all eight; a `loader.pixel_dtype = ...` answers
                # one and leaves the rest.
                #
                # This sits *above* the revision guard below, so it reads
                # `inst.attributes` as they are now rather than as they
                # were at the caller's capture. That is harmless: a moved
                # revision leaves the instance dirty
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
                    # below): without it a `set_pixel_data()` landing
                    # after the read would have its flag cleared here
                    # while its array is still unwritten, and a discard
                    # leaves nothing to clear. Checked under the
                    # pixel-state leaf, where both mutators move the
                    # revision.
                    if (inst.pixel_array is arr
                            and (revision is None or inst._revision == revision)):
                        # These exact bytes are already in the sidecar
                        # and the loader already points at them, so the
                        # resident array is recoverable and freeable
                        # again.
                        inst._pixel_array_unwritten = False
                        # The loader just rebuilt describes them under
                        # the current descriptors, so a discard has
                        # nothing to put back -- a same-bytes,
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
            # read after an unload would raise an integrity mismatch
            # against correctly-saved data. Passing it removes the ordering
            # dependency between this call and the assignment below.
            written = self._create_pixel_loader(
                offset, length, _PIXEL_COMPRESSION, inst, pixel_hash=digest)
            # The guard and the publish under the pixel-state leaf, and
            # the guard inside it: outside, a `discard_pixel_data()`
            # landing after the check and before the clears would restore
            # the pre-set descriptors over the frame being published, and
            # a `set_pixel_data()` there would have its flag cleared by a
            # save of the previous array. Both mutators move the
            # revision under the same lock, so the guard sees them. The
            # loader is built before it: construction reads attributes
            # and is wasted only when the guard skips.
            with entities.PIXEL_STATE_LOCK:
                if revision is not None and inst._revision != revision:
                    # The bytes read above no longer describe the instance:
                    # a mutation (a redaction, most importantly) landed after
                    # the caller's capture. Publishing them would write a row
                    # and a loader for state the graph has already left --
                    # a redacted instance rewired to unredacted pixels. The
                    # frame already appended is
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
                # the sweep only logs counts.
                inst._pixel_array_unwritten = False
                # Written, so it is the stored frame and a discard has
                # nothing to undo. After the revision guard above,
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
        """The number of instances currently persisted.

        Returns:
            int: The count of rows in the `instances` table; 0 on a
                database error, which is logged.
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
        """An iterator of one flat dictionary per stored instance.

        For streaming exports or analysis without loading the entire graph
        into RAM. The rows come back one page at a time, and **no database
        handle is held between pages**, so an iterator left part-consumed
        holds no lock and no read snapshot. Two consequences:

        - **Iteration is not one snapshot.** Each page is its own query, so
          writes that land between pages are visible and rows deleted between
          pages are not returned.
        - **Order is by `instances.id`.**

        Args:
            patient_ids (Iterable[str], optional): Restrict the rows to
                these Patient IDs, read as every `patient_ids` in the
                package is read. ``None`` means every patient in the store. An
                empty iterable matches nobody: it is a filter that selected
                nothing, not an absent filter. An iterator is read once, at the
                call. An ID no patient holds is **not counted** here, unlike the
                `Session` methods: the pages are separate reads, so there is no
                single moment at which an ID is or is not held.
            instance_uids (Iterable[str], optional): Restrict the rows to
                these SOP Instance UIDs. Same rules: ``None`` is no
                filter, an empty iterable matches nobody. Both filters
                together intersect.
            page_size (int, optional): Rows per page, defaulting to 500.
                Trades resident memory against the number of queries.
                Must be an `int` >= 1.

        Returns:
            Iterator[dict]: One dict per instance, keyed `patient_id`,
                `patient_name`, `study_instance_uid`, `study_date`,
                `series_instance_uid`, `modality`, `series_number`,
                `manufacturer`, `model_name`, `device_serial_number`,
                `sop_instance_uid`, `sop_class_uid`, `instance_number`,
                `file_path`, `pixel_offset`, `pixel_length`, `compress_alg` and
                `attributes_json`, with the values as stored.

        Raises:
            ValueError: If `page_size` is not a whole number >= 1, at the call,
                not on the first `next()`.
            TypeError: If either filter is a bare `str`, bytes-like, not
                iterable, or holds an element that is not a `str` (named
                by position and type, never by value), at the call.
                `page_size` is checked first.
        """
        # `bool` before `int`, and the ordering is the mechanism:
        # `isinstance(True, int)` is True and `True < 1` is False, so
        # `page_size=True` would pass a bare `isinstance(page_size, int)`
        # -- and then page one row at a time, silently. Same subclass
        # trap, and the same answer, as `_fallback_encoding`'s `bool` arm
        # and `_value_fits_vr`'s ordering in `io_handlers.py`.
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
        # Here, in the plain method, and not in the generator below, for
        # the reason `page_size` is: a refusal fires at the call, not on
        # the first `next()`. Materialised to tuples, because the walk
        # builds its placeholders from them and then binds them -- a
        # generator would be consumed by the first and raise
        # `ProgrammingError` at the second.
        patient_ids = normalize_id_filter(patient_ids, "patient_ids")
        instance_uids = normalize_id_filter(instance_uids, "instance_uids",
                                            kind="SOP Instance UID")
        return self._iter_flattened_instances(
            patient_ids, instance_uids, page_size)

    def _iter_flattened_instances(self, patient_ids, instance_uids, page_size):
        """Keyset walk backing `get_flattened_instances`.

        Args:
            patient_ids: Normalized Patient ID filter, or None for none.
            instance_uids: Normalized SOP Instance UID filter, or None.
            page_size (int): Rows per query.

        Yields:
            dict: One row, in `get_flattened_instances`'s shape.
        """
        # Each page opens its own `_get_connection`, so the lock (or the
        # connection, on a file store) is held for the query and nothing
        # else. Paging by re-query rather than by `fetchmany` on a live
        # cursor is not a preference: on the file path `_get_connection`
        # **closes** the connection when its block exits, so a cursor
        # cannot survive to a second page at all.
        #
        # The keyset is `instances.id`, an INTEGER PRIMARY KEY and so the
        # rowid, so resuming is a seek rather than an OFFSET scan. It is
        # selected first and stripped off before yielding -- a walk
        # cursor, not part of the published row shape.
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
        # everyone. A caller computing a cohort that came back empty
        # would otherwise walk the whole store -- silent over-export.
        # Same rule as `get_cohort_report` and `_export_dicom` in
        # `session.py`.
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
        """Efficiently updates the attributes_json for a list of instances.

        Used when only attributes have changed (e.g., after locking identities)
        to avoid full graph traversal.

        One call is one transaction: a failure writes none of `instances`.
        Nothing marks them persisted either way, so an instance whose write
        failed still reads as holding unsaved changes and a later `save()`
        writes it.

        Args:
            instances (List[Instance]): The list of instances to update.

        Raises:
            sqlite3.Error: The write failed. Logged, recorded as one
                `ERROR` audit row (best-effort: a row that cannot be
                recorded does not replace this exception), and re-raised
                as sqlite raised it.
            RuntimeError: The write matched fewer rows than `instances`
                holds: some instance's current SOP Instance UID has no row
                in the store (a graph built by hand and never saved, or a UID
                `regenerate_uid()` moved since the last save). The write is
                rolled back, so it stores none of `instances`, then logged and
                recorded as one `ERROR` row (best-effort, as above), counts
                only. Two limits: two instances sharing one SOP Instance UID,
                which only a hand-built graph can hold, both match its one row,
                so no shortfall is seen and the row holds whichever was written
                last; and the check counts the rows the write matched, it does
                not read them back.
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

                # **A write that lands on no row is a failure too.**
                # An instance whose current SOP Instance UID the store
                # holds no row for -- a hand-built graph never saved, a UID
                # `regenerate_uid()` moved and no save has written since --
                # matches nothing, and sqlite raises nothing for that, so
                # without this `lock_identities(persist=True)` would report
                # the lock secured while the store held no token. `rowcount`
                # after `executemany` is summed over the parameter sets and
                # counts a matched row whether or not its value changed, and
                # a row a `RAISE(IGNORE)` trigger skipped is not counted (the
                # same on 3.12 and 3.14t). So it counts rows written, not
                # rows that exist. Raised **here, inside the `with`,
                # before `commit()`**, so `_get_connection` rolls the whole
                # write back on either backend (`:memory:` commits on its
                # one shared connection, and rolls it back the same way):
                # the instances that did match are not stored alone, and
                # the row below says "stored none of them" truthfully. The
                # row is written outside the `with`: on `:memory:` the
                # connection's lock is held inside it.
                shortfall = len(data) - cur.rowcount
                if shortfall:
                    raise _NoRowFor(shortfall, len(data))

                conn.commit()
                self.logger.info("Update complete.")

        except _NoRowFor as missing:
            # Counts only, no UID and no patient. `RuntimeError`, not an
            # invented `sqlite3.Error`: sqlite raised nothing. The same
            # post-embed timing as the arm below, which the callers'
            # docstrings state.
            message = (f"update_attributes: {missing.shortfall} of {missing.total} "
                       "instance(s) have no row in the store under their SOP "
                       "Instance UID, so this write stored none of them; "
                       "save(sync=True) writes an instance the store does not "
                       "hold yet")
            self.logger.error(message)
            try:
                self.log_audit(action_type="ERROR", entity_uid="SESSION",
                               details=message)
            except Exception as row_error:  # pylint: disable=broad-except
                self.logger.error(
                    "update_attributes could not record its failure in the "
                    f"audit log: {describe_exception(row_error)}")
            raise RuntimeError(message) from None

        except sqlite3.Error as e:
            # Raised, not swallowed. The caller is a lock that has already
            # embedded the token in memory; a log line alone would let
            # `lock_identities(persist=True)` return success while the
            # store held no token, and a reopen would lose the way back
            # with nothing in the session's story to say so. The row is the
            # durable half and is best-effort: `except Exception`, so a
            # store too broken to take a row still hands the caller the
            # error that failed the write. A count and sqlite's own text
            # (bound values never reach it); no SOP UID, no patient. "This
            # write stored none of them", and not "held in memory only":
            # `lock_identities(persist=True, auto_persist_chunk_size=N)`
            # writes each instance twice, and where only the second write
            # fails the store already holds the token. The row speaks for
            # this transaction, not for the store.
            message = (f"update_attributes could not write {len(instances)} "
                       "instance(s) to the store, so this write stored none "
                       f"of them: {describe_exception(e)}")
            self.logger.error(message)
            try:
                self.log_audit(action_type="ERROR", entity_uid="SESSION",
                               details=message)
            except Exception as row_error:  # pylint: disable=broad-except
                self.logger.error(
                    "update_attributes could not record its failure in the "
                    f"audit log: {describe_exception(row_error)}")
            raise

    def save_findings(self, findings: List[PhiFinding]):
        """Append PHI findings to the `phi_findings` table.

        A database error is logged and not raised; the findings are then
        not stored.

        Args:
            findings (List[PhiFinding]): The findings to insert. Empty
                writes nothing.
        """
        timestamp = datetime.now().isoformat()

        if not findings:
            return

        self.logger.info(f"Saving {len(findings)} PHI findings...")

        try:
            with self._get_connection() as conn:
                cur = conn.cursor()

                # A generator, so the batch insert never holds a second copy.
                def findings_generator():
                    """One `phi_findings` row per finding.

                    Yields:
                        tuple: The ten column values, in the INSERT's order.
                    """
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
        """Load every stored PHI finding, in insertion order.

        A loaded finding carries no entity reference, and its remediation
        proposal (if any) no original value. A database error is logged and
        whatever was read before it is returned.

        Returns:
            List[PhiFinding]: The stored findings; empty for a file store
                whose database file does not exist.
        """
        findings = []
        if self.db_path != ":memory:" and not os.path.exists(self.db_path):
            return findings

        try:
            with self._get_connection() as conn:
                cur = conn.cursor()
                # No table-exists check: `_init_db` creates the schema in
                # `__init__`.

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
        """Reclaim disk space by rewriting the sidecar file.

        Copies every live blob (one whose instance row exists) into a new
        file, swaps it in, deletes the orphaned `instance_blobs` rows and
        rewrites every stored offset. A failure before the database update
        commits restores the original sidecar and leaves the database as it
        was, apart from the legacy back-fill `_read_blob_index` commits
        first; one after it (removing the backup) leaves the compacted file
        and the committed offsets in place. Takes no lock of its
        own: `Session.compact()` holds the sidecar gate across this call and
        the loader rewire after it, and in-memory loaders still point at the
        old offsets until they are rewired.

        Returns:
            Dict[str, Tuple[int, int]]: SOP Instance UID -> new
                `(offset, length)`, for top-level pixel blobs only; empty when
                nothing is live (the file is then left as it is).

        Raises:
            sqlite3.Error: Propagated from `_read_blob_index()` when the
                index read fails, before the rewrite starts; the sidecar
                is untouched and there is nothing to discard.
            OSError: Propagated from `os.path.getsize` on the sidecar,
                before the rewrite starts; the sidecar is untouched.
            Exception: Any failure rewriting the file, swapping it in or
                updating the database, re-raised after the working files
                are discarded.
            BaseException: Anything that interrupts the database update,
                `KeyboardInterrupt` included, re-raised after the original
                sidecar is swapped back.
        """
        # The file is rewritten first and the database updated second. The
        # two hold the same fact in two places: a database describing a
        # layout the file does not hold produces silent garbage rather than
        # an error, because every read lands at a plausible-looking wrong
        # offset. The ordering, and the rollback in each direction, keep the
        # two on the same generation whatever fails.
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

            # No `self.sidecar = SidecarManager(...)` rebind here: it would
            # be inert -- `SidecarManager` holds only `filepath`, and
            # `write_frame`/`read_frame` open by path on every call -- and
            # would read as though it re-pointed a writer at the compacted
            # file, a guarantee this code cannot make: a concurrent writer
            # holds whatever manager it already read. What does close that
            # is the sidecar gate, which `Session.compact()` holds across
            # this method AND the loader rewire after it; this method takes
            # no lock of its own, deliberately, so the hold and the rewire
            # cannot be split. A direct caller of this method gets the
            # orphan predicate and nothing else.
            self._log_compaction_result(start_time, original_size, written_bytes)
            return uid_map

        except Exception as exc:
            self.logger.error(f"Compaction Failed: {describe_exception(exc)}")
            self._discard_compaction_artefacts(temp_path, backup_path)
            raise

    def _read_blob_index(self):
        """Reads which sidecar blobs are still live, and which are orphans.

        A blob is live while its instance row exists. Back-fills legacy
        `instances.pixel_*` references into `instance_blobs` first.

        Returns:
            Tuple of (live rows ordered by offset, orphan `instance_blobs.id`
                values as single-element tuples ready for `executemany`).

        Raises:
            sqlite3.Error: The read failed; logged, then re-raised.
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

        A frame shorter on disk than its row says is copied as read, with a
        warning. Rows of length 0 or less are skipped.

        Args:
            rows: Live rows from `_read_blob_index`, ordered by offset, so
                the read head only moves forward.
            temp_path (str): The file to write.

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

        Args:
            temp_path (str): The rewritten file.
            backup_path (str): Where the original goes; an existing file
                there is removed first.
        """
        # The paths share a directory by construction, so `os.replace` is
        # atomic and these renames cost nothing.
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

        Deletes the orphan `instance_blobs` rows and writes the new offset
        and length to each live blob row and, for pixel blobs, to its
        `instances` row.

        Args:
            orphan_ids: `(instance_blobs.id,)` tuples to delete.
            updates: `(new_offset, new_length, instance_blobs.id)` per live
                row -- the blob table's id, not `instances.id`.
        """
        # `instances` is patched by UID through a lookup on the blob table:
        # updating it by the blob id would corrupt unrelated rows. Both
        # columns are written together: an offset from the new generation
        # beside a length from the old one reads as truncated data rather
        # than as an error.
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


def _tag_number_strings(obj):
    """`obj` with every DS and IS atom replaced by its tagged text.

    Args:
        obj: A value, or a dict/list/tuple/`MultiValue` of values, at any
            depth. Never modified.

    Returns:
        The same shape in new containers, each DS or IS atom replaced by
            `{"__type__": "DS"|"IS", "data": <its text>}`; other values as
            given.
    """
    # New containers, never an edit of `obj`'s: `_serialize_item` hands
    # this a shallow copy whose lists are the graph's own.
    # `IS` before anything numeric: `IS` is an `int`, `ISfloat` (what
    # `IS("1.5")` builds) a `float`, and both are text a reader wrote.
    if isinstance(obj, (IS, ISfloat)):
        return {"__type__": "IS", "data": str(obj)}
    if isinstance(obj, (DSfloat, DSdecimal)):
        return {"__type__": "DS", "data": str(obj)}
    if isinstance(obj, dict):
        return {k: _tag_number_strings(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, MultiValue)):
        return [_tag_number_strings(v) for v in obj]
    return obj


class IsocenterJSONEncoder(json.JSONEncoder):
    """How a graph value becomes `attributes_json`, and the one place that decides.

    DS and IS values are written as `{"__type__": "DS"|"IS", "data": <their
    text>}`, at every depth and inside every `MultiValue`, so their text
    survives (`'5.000000'` does not come back as `5.0`); `bytes` as
    `{"__type__": "bytes", "data": <base64>}`; a `MultiValue` as a list.
    `isocenter_json_object_hook` turns the tagged values back.
    """

    def iterencode(self, o, _one_shot=False):
        """Encode `o` with every DS and IS atom tagged first.

        Args:
            o (Any): The value to encode.

        Returns:
            Iterator[str]: The encoded chunks.
        """
        # `json` encodes a `float` or `int` subclass natively and never
        # calls `default()` for it, so DS and IS have to be tagged here,
        # before encoding, or their text is lost. `encode` calls this.
        return super().iterencode(_tag_number_strings(o), _one_shot)

    def default(self, obj):
        """Encode a value `json` cannot: `bytes` and `MultiValue`.

        Args:
            obj (Any): The value `json` could not encode.

        Returns:
            dict | list: `{"__type__": "bytes", "data": <base64>}` for
                `bytes`, a list for a `MultiValue`.

        Raises:
            TypeError: For any other type (from `json.JSONEncoder.default`).
        """
        if isinstance(obj, bytes):
            return {"__type__": "bytes", "data": base64.b64encode(obj).decode('ascii')}

        if isinstance(obj, MultiValue):
            return list(obj)

        return super().default(obj)


def isocenter_json_object_hook(d):
    """A tagged value back into what the graph held.

    Pass it as `object_hook` to every `json.loads` of `attributes_json`
    that needs values; without it a DS, IS or bytes value reads back as
    its tag dictionary.

    DS and IS are rebuilt without validation or warnings: the text is what
    ingest already accepted. A `DSdecimal` comes back a `DSfloat` with the
    same text.

    Args:
        d (dict): A decoded JSON object.

    Returns:
        bytes | DSfloat | IS | dict: The value for a tagged dict; `d`
            unchanged otherwise.
    """
    # Built under `IGNORE`, so a reload neither refuses nor re-warns about
    # text ingest accepted. The two `attributes_json` readers that pass no
    # hook (the Rows/Columns read in the blob index, and the key scan) read
    # no DS, IS or bytes value; one that did would get the tag dictionary.
    kind = d.get("__type__")
    if kind == "bytes":
        return base64.b64decode(d["data"])
    if kind == "DS":
        return DSfloat(d["data"], validation_mode=pydicom_config.IGNORE)
    if kind == "IS":
        return IS(d["data"], validation_mode=pydicom_config.IGNORE)
    return d

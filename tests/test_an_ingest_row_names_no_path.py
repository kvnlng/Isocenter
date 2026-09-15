"""An ingest row names a source file by a key, never by its path (#591).

Source trees are very often named for the patient -- `Smith_John_MRN4455/`
-- and ingest runs before any de-identification can. Measured on 76385a5
under such a folder, 3.12 and 3.14t alike:

- a rejected file's `ERROR` row carried the full path as its
  `entity_uid`, and again in its detail: `Ingest failed for
  /.../Smith_John_MRN4455/notes.txt: ValueError: Missing SOPInstanceUID.`;
- an unreadable file's reason repeated it, because `OSError.__str__`
  appends the filename: `PermissionError: [Errno 13] Permission denied:
  '/.../Smith_John_MRN4455/locked.dcm'`;
- the #431 duplicate row named both files by path, and the #238
  superseded row the declined one;
- the compliance report rendered every one of those details, and the
  logger's console handler printed each `ERROR` and `WARNING` line to
  **stdout**.

Now the row is keyed by the first eight hex digits of the sha256 of the
path's filesystem bytes (as ruled on #591), and the
path goes to exactly two places: `IngestSummary.failures`, which the caller
already holds, and `isocenter.log`, at INFO, in a line pairing it with its
key. The console handler is WARNING and up, so the pairing line never
reaches stdout. Reasons are spelled without paths.

The key is a digest, not a secret: anyone who can guess a file's full path
can recompute it. That is the ruling, and the derivation is one helper so a
keyed form is a one-line change.

The same owner comment on #591 named the pixel-read doors, and they are
here too: `get_pixel_data()`'s `FileNotFoundError` named the source path,
and the redaction and OCR reasons spelled a read failure's cause whole.

**Fixtures put their sources in a `MARK` subfolder and ingest its parent.**
`Session.ingest` prints `Ingesting from '<directory>'...`, the caller's own
argument, and that line is kept deliberately (as E1 kept the export
folder); ingesting the `MARK` folder itself would fail every stdout
assertion for a reason this change does not touch. The log file is set
before the session is built, because every `Session()` replaces the
logger's handlers (#611).
"""
import errno
import hashlib
import os
import shutil
import sqlite3

import pydicom
import pytest
from pydicom.data import get_testdata_file

from isocenter import io_handlers, pixel_analysis
from isocenter.entities import Instance
from isocenter.io_handlers import ingest_worker
from isocenter.logger import describe_exception_without_paths
from isocenter.services import RedactionError, RedactionService
from isocenter.session import DicomSession
from isocenter.store import DicomStore
from tests.test_reingest_after_redact import RULES, _write_source
from tests.test_wfdb_writer import _no_sample_waveform_instance

#: The patient-named folder every source sits in.
MARK = "Doe_Jane_MRN4455"
CT_STORAGE = "1.2.840.10008.5.1.4.1.1.2"


def _key(path):
    """The key, derived here independently of the helper under test."""
    return hashlib.sha256(os.fsencode(path)).hexdigest()[:8]


@pytest.fixture
def log_file(tmp_path, monkeypatch):
    """`isocenter.log` for this test, at the default level."""
    path = tmp_path / "isocenter.log"
    monkeypatch.setenv("ISOCENTER_LOG_FILE", str(path))
    monkeypatch.delenv("ISOCENTER_LOG_LEVEL", raising=False)
    return path


def _rows(session, action_type=None):
    """`(action_type, entity_uid, details)` rows, read through the barrier."""
    session.store_backend.flush_audit_queue()
    with sqlite3.connect(session.persistence_file) as conn:
        query = "SELECT action_type, entity_uid, details FROM audit_log"
        if action_type is None:
            return conn.execute(query).fetchall()
        return conn.execute(query + " WHERE action_type=?",
                            (action_type,)).fetchall()


def _source_folder(tmp_path, name="root"):
    """`<tmp>/<name>/MARK/`, created; returns `(root, marked)`."""
    root = tmp_path / name
    marked = root / MARK
    marked.mkdir(parents=True)
    return root, marked


def _with_junk(tmp_path):
    """A good CT and a file that is not DICOM, under the MARK folder."""
    root, marked = _source_folder(tmp_path)
    shutil.copy(get_testdata_file("CT_small.dcm"), marked / "good.dcm")
    (marked / "junk.dcm").write_bytes(
        b"this is not a DICOM file, whatever the extension says")
    return root


def _report(session, tmp_path):
    path = tmp_path / "report.md"
    session.generate_report(str(path))
    return path.read_text()


def test_a_rejected_file_is_keyed_by_the_hash_of_its_path(tmp_path, log_file):
    """The row's entity is the key of exactly the summary's path.

    Equality with a digest recomputed from `summary.failures`, not `len ==
    8`: a basename's hash or a folder's hash has eight characters too, and
    the point is that a caller holding the summary can find the row.
    """
    root = _with_junk(tmp_path)
    with DicomSession(str(tmp_path / "s.db")) as session:
        summary = session.ingest(str(root))
        (path, reason), = summary.failures
        key = _key(path)
        (row,) = _rows(session, "ERROR")
        report = _report(session, tmp_path)

    assert row[1] == key
    assert row[2] == f"Ingest failed for the file keyed {key}: {reason}"
    assert MARK not in row[1] and MARK not in row[2]
    assert key in report
    assert MARK not in report


def test_the_path_reaches_the_summary_and_the_log_file_and_nothing_else(
        tmp_path, log_file, capfd):
    """Two homes for the path: the summary and `isocenter.log`.

    `capfd`, so both the console handler's stdout and anything on stderr
    are read. Built after `capfd` is active, so the console handler holds
    the captured stream.
    """
    root = _with_junk(tmp_path)
    with DicomSession(str(tmp_path / "s.db")) as session:
        summary = session.ingest(str(root))
        (path, _), = summary.failures
        rows = _rows(session)

    captured = capfd.readouterr()
    assert MARK in path
    assert MARK not in captured.out, captured.out
    assert MARK not in captured.err, captured.err
    assert all(MARK not in (details or "") for _, _, details in rows), rows
    pairing = [line for line in log_file.read_text().splitlines()
               if _key(path) in line and path in line]
    assert pairing, "isocenter.log does not pair the key with its path"


def test_an_os_error_reason_names_no_path(tmp_path):
    """The worker's blanket `except`: an `OSError` is spelled by `strerror`."""
    missing = str(tmp_path / MARK / "missing.dcm")

    result = ingest_worker(missing)

    assert result[1] is None
    assert result[7] == "FileNotFoundError: No such file or directory"
    assert MARK not in result[7]


def test_a_decompression_failure_reason_names_no_path(tmp_path, monkeypatch):
    """The worker's decode arm, which spells its reason on its own line.

    A decoder reading the source file can raise an `OSError` built on its
    path; the arm is not the blanket `except`, so it needs its own test.
    """
    marked = tmp_path / MARK
    marked.mkdir()
    source = str(marked / "ct.dcm")
    shutil.copy(get_testdata_file("CT_small.dcm"), source)

    def unreadable(*_args, **_kwargs):
        raise OSError(errno.EIO, "Input/output error", source)

    monkeypatch.setattr(io_handlers, "_decode_pixels", unreadable)
    result = ingest_worker(source)

    assert result[1] is None
    assert result[0] == {"path": source}
    assert result[7] == "Decompression Failed: OSError: Input/output error"


@pytest.mark.skipif(os.geteuid() == 0,
                    reason="root reads a mode-000 file, so nothing fails")
def test_an_unreadable_file_names_no_path_in_the_summary_or_the_row(
        tmp_path, log_file):
    """The session-level twin: the summary reason and the row, both clean."""
    root, marked = _source_folder(tmp_path)
    locked = marked / "locked.dcm"
    shutil.copy(get_testdata_file("CT_small.dcm"), locked)
    os.chmod(locked, 0)
    try:
        with DicomSession(str(tmp_path / "s.db")) as session:
            summary = session.ingest(str(root))
            (path, reason), = summary.failures
            (row,) = _rows(session, "ERROR")
    finally:
        os.chmod(locked, 0o600)

    assert reason == "PermissionError: Permission denied"
    assert row[2] == f"Ingest failed for the file keyed {_key(path)}: {reason}"


def test_the_key_survives_an_undecodable_filename():
    """`os.fsencode`, not `.encode()`: a surrogate escape must not raise.

    `os.walk` hands back an undecodable filename byte as a surrogate
    escape on POSIX, and `str.encode("utf-8")` raises on it -- inside
    `_record_failure`, replacing the failure being recorded.
    """
    path = "/x/" + b"\xff.dcm".decode("utf-8", "surrogateescape")

    assert io_handlers._ingest_file_key(path) == _key(path)


def _two_copies(tmp_path):
    root, marked = _source_folder(tmp_path)
    shutil.copy(get_testdata_file("CT_small.dcm"), marked / "a.dcm")
    shutil.copy(get_testdata_file("CT_small.dcm"), marked / "b.dcm")
    return root, str(marked / "a.dcm"), str(marked / "b.dcm")


def test_a_duplicate_names_both_files_by_key(tmp_path, log_file):
    """#431's row: the declined file and its holder, both by key.

    `a.dcm` sorts first and is kept (#450), so `b.dcm` is declined.
    """
    root, kept, declined = _two_copies(tmp_path)
    uid = pydicom.dcmread(kept).SOPInstanceUID
    with DicomSession(str(tmp_path / "s.db")) as session:
        summary = session.ingest(str(root))
        (row,) = _rows(session, "WARNING")

    assert (summary.ingested, summary.declined) == (1, 1)
    assert row[2].startswith(
        f"Not importing the file keyed {_key(declined)}: SOP Instance UID "
        f"{uid} is already held by the instance ingested from the file "
        f"keyed {_key(kept)}. "), row[2]
    assert MARK not in row[2]
    log = log_file.read_text()
    for path in (kept, declined):
        assert any(_key(path) in line and path in line
                   for line in log.splitlines()), path


def test_a_superseded_source_is_named_by_key(tmp_path, log_file):
    """#238's row: the un-redacted original, offered again from elsewhere."""
    root, marked = _source_folder(tmp_path)
    _write_source(marked / "a.dcm", "1.2.3.phi")
    elsewhere, copy_dir = _source_folder(tmp_path, name="elsewhere")
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(str(root))
        session.configuration.rules = RULES
        assert session.redact(show_progress=False) == 1
        shutil.copy(marked / "a.dcm", copy_dir / "copy.dcm")
        session.ingest(str(elsewhere))
        (row,) = _rows(session, "WARNING")

    copy = str(copy_dir / "copy.dcm")
    assert row[2].startswith(f"Not importing the file keyed {_key(copy)}: "
                             "SOP Instance UID 1.2.3.phi is"), row[2]
    assert MARK not in row[2]


def test_a_linkage_failure_is_keyed_and_spelled_without_paths(
        tmp_path, log_file, monkeypatch):
    """A parent-side failure takes the same route, and the same spelling."""
    root, marked = _source_folder(tmp_path)
    shutil.copy(get_testdata_file("CT_small.dcm"), marked / "ct.dcm")

    def refuses(*args, **kwargs):
        raise OSError(errno.EACCES, "Permission denied",
                      str(tmp_path / MARK / "x"))

    monkeypatch.setattr(io_handlers.Equipment, "from_parts", refuses)
    with DicomSession(str(tmp_path / "s.db")) as session:
        summary = session.ingest(str(root))
        (path, reason), = summary.failures
        (row,) = _rows(session, "ERROR")

    assert reason == "Linkage Failed: PermissionError: Permission denied"
    assert row[1] == _key(path)
    assert MARK not in row[2]


def test_a_missing_source_file_names_the_instance(tmp_path):
    """`get_pixel_data()`'s last raise names the instance, errno form.

    The two-argument form is load-bearing: it sets `strerror`, so
    `describe_exception_without_paths` keeps the words. A one-argument
    `FileNotFoundError(message)` has none and is spelled as the bare type.
    """
    uid = "1.2.826.0.1.591.8"
    inst = Instance(uid, CT_STORAGE, 1,
                    file_path=str(tmp_path / MARK / "gone.dcm"))

    with pytest.raises(FileNotFoundError) as raised:
        inst.get_pixel_data()

    assert MARK not in str(raised.value)
    assert uid in str(raised.value)
    assert describe_exception_without_paths(raised.value) == (
        f"FileNotFoundError: Pixels missing and file not found for "
        f"instance {uid}")


def _unreadable(tmp_path, monkeypatch):
    """An instance whose pixel read fails on a patient-named path.

    The shape `get_pixel_data()` raises for an unreadable source since E1:
    a path-free `RuntimeError` whose *cause* is the `OSError`, and the
    cause's `str()` carries the path.
    """
    uid = "1.2.826.0.1.591.9"
    inst = Instance(uid, CT_STORAGE, 1, file_path=None)
    source = str(tmp_path / MARK / "x.dcm")

    def unreadable(self):
        try:
            raise PermissionError(errno.EACCES, "Permission denied", source)
        except PermissionError as exc:
            raise RuntimeError(
                f"Lazy load failed for instance {self.sop_instance_uid}"
            ) from exc

    monkeypatch.setattr(Instance, "get_pixel_data", unreadable)
    return inst


def test_a_redaction_failure_reason_names_no_path(tmp_path, monkeypatch,
                                                  capfd):
    """The parallel worker's outcome, and nothing on stderr.

    `traceback.print_exc()` printed the exception whole, cause included;
    the traceback now goes to the log at DEBUG.
    """
    inst = _unreadable(tmp_path, monkeypatch)
    service = RedactionService(DicomStore())
    task = {"instance": inst, "original_sop_uid": inst.sop_instance_uid,
            "rois": [(0, 4, 0, 4)], "config_hash": "591"}

    outcome = service.execute_redaction_task(task)

    captured = capfd.readouterr()
    assert outcome.ok is False
    assert outcome.error.startswith("RuntimeError: Lazy load failed")
    assert MARK not in outcome.error
    assert MARK not in captured.err, captured.err
    assert MARK not in captured.out, captured.out


def test_a_sequential_redaction_failure_reason_names_no_path(tmp_path,
                                                             monkeypatch):
    """`redact_machine_instances`' failure list, the #213 channel."""
    inst = _unreadable(tmp_path, monkeypatch)
    service = RedactionService(DicomStore())

    with pytest.raises(RedactionError) as raised:
        service.redact_machine_instances("SN-591", [(0, 4, 0, 4)],
                                         targets=[inst], show_progress=False)

    (uid, detail), = raised.value.failures
    assert uid == inst.sop_instance_uid
    assert "Lazy load failed" in detail
    assert MARK not in detail


def test_a_died_redaction_worker_names_no_path(tmp_path):
    """`_apply_redaction_outcomes`' arm for a worker that never answered."""
    try:
        raise PermissionError(errno.EACCES, "Permission denied",
                              str(tmp_path / MARK / "x.dcm"))
    except PermissionError as exc:
        died = exc

    _, failures = DicomSession._apply_redaction_outcomes([died], {})

    (uid, detail), = failures
    assert uid == "UNKNOWN"
    assert detail == "Redaction worker failed: PermissionError: Permission denied"


def test_an_unreadable_pixel_source_scan_reason_names_no_path(tmp_path,
                                                              monkeypatch):
    """The pre-export OCR scan's read reason, which reaches a WARNING row."""
    inst = _unreadable(tmp_path, monkeypatch)

    result = pixel_analysis._load_and_ocr(inst)

    assert result.failure.startswith("pixels could not be read: RuntimeError: ")
    assert MARK not in result.failure


def test_a_wfdb_record_with_no_uid_is_keyed_unknown(tmp_path, caplog):
    """A WFDB `DATA_LOSS` row never falls back to the source path.

    The export plan names every record after its UID, so a UID-less
    instance takes a graph built by hand. Keyed `UNKNOWN`, as the DICOM
    side's rows are (E1).
    """
    import logging

    from isocenter.exporters.wfdb import WfdbExporter

    patient, study, series, instance = _no_sample_waveform_instance(uid="")
    instance.source_path = str(tmp_path / MARK / "ecg.dcm")
    session = DicomSession(str(tmp_path / "wfdb.db"))
    try:
        with caplog.at_level(logging.WARNING):
            WfdbExporter()._write_instance(
                str(tmp_path / "out"), patient, study, series, instance,
                logging.getLogger("isocenter.wfdb_no_uid_test"), {},
                store_backend=session.store_backend)
        rows = _rows(session, "DATA_LOSS")
    finally:
        session.close()

    (row,) = rows
    assert row[1] == "UNKNOWN"
    assert MARK not in row[2]
    assert all(MARK not in record.getMessage() for record in caplog.records)

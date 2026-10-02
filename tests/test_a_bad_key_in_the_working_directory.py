"""`Session()` over a bad `./isocenter.key` names the file, releases what
it started, and refuses a path that is not a regular file (#791, owner
ruling Q1 A).

`Session()` enables reversible anonymization with any `isocenter.key` in
the working directory, unasked (frozen at 1.0). Measured on b440bca0:

- a malformed key raised `Fernet key must be 32 url-safe base64-encoded
  bytes.`, naming no file, so the user could not tell which was at fault;
- the half-built session's `AuditWorker` and `PersistenceWorker` threads
  were still alive after the raise, for as long as anything referenced the
  failed session (its traceback, `sys.last_exc` in a REPL: indefinitely).
  They hold their owners by weakref, so a test that sleeps, or drops the
  exception before looking, is green on the bug; every thread check here
  reads while `exc_info` still holds the frame, with no sleep;
- a directory at the path raised `IsADirectoryError`, not the frozen
  `ValueError`;
- a FIFO there made `Session()` block forever in `open()`.

Every construction is written as `with pytest.raises(...): with
Session(...)`, for #371's construction-site check.
"""
import logging
import os
import stat
import subprocess
import sys
import threading

import pytest
from cryptography.fernet import Fernet
from pydicom.data import get_testdata_file

from isocenter import Session
from isocenter.crypto import KeyManager
from isocenter.persistence_manager import PersistenceManager

WORKERS = ("AuditWorker", "PersistenceWorker")


def _new_workers(before):
    return [t for t in threading.enumerate()
            if t.name in WORKERS and t not in before]


def _half_built_threads(exc_info):
    """The failed session's two worker threads, read off the traceback.

    Taken from the session itself rather than from a census, so a check
    that finds nothing alive is not blind: these are the threads, and on
    the bug they are alive.
    """
    tb = exc_info.tb
    while tb is not None:
        candidate = tb.tb_frame.f_locals.get("self")
        if isinstance(candidate, Session):
            return [candidate.persistence_manager.thread,
                    candidate.store_backend._audit_thread]
        tb = tb.tb_next
    raise AssertionError("the traceback holds no Session")


def _malformed(path):
    with open(path, "wb") as fh:
        fh.write(b"not a key")


def _empty(path):
    open(path, "wb").close()


def _directory(path):
    os.mkdir(path)


def _valid(path):
    with open(path, "wb") as fh:
        fh.write(Fernet.generate_key())
    os.chmod(path, 0o600)


def test_a_malformed_key_names_its_path():
    _malformed("isocenter.key")
    expected = os.path.abspath("isocenter.key")
    with pytest.raises(ValueError) as exc_info:
        with Session("s.db"):
            pass
    assert expected in str(exc_info.value)
    assert "does not hold a Fernet key" in str(exc_info.value)


@pytest.mark.parametrize("make", [_malformed, _empty, _directory],
                         ids=["malformed", "empty", "directory"])
def test_the_threads_are_released_before_the_raise(make):
    make("isocenter.key")
    before = set(threading.enumerate())
    with pytest.raises(ValueError) as exc_info:
        with Session("s.db"):
            pass
    # Read while `exc_info` holds the traceback, and so the half-built
    # session: on the bug its workers live exactly as long as that does.
    threads = _half_built_threads(exc_info)
    assert sorted(t.name for t in threads) == sorted(WORKERS)
    assert [t.name for t in threads if t.is_alive()] == []
    assert [t.name for t in _new_workers(before) if t.is_alive()] == []


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0,
                    reason="root reads a mode-000 file")
def test_any_exception_releases_the_threads():
    """A mode-000 key raises `PermissionError` (an `OSError`, unchanged),
    and the threads are released as for a `ValueError`."""
    _valid("isocenter.key")
    os.chmod("isocenter.key", 0)
    try:
        before = set(threading.enumerate())
        with pytest.raises(PermissionError) as exc_info:
            with Session("s.db"):
                pass
        threads = _half_built_threads(exc_info)
        assert sorted(t.name for t in threads) == sorted(WORKERS)
        assert [t.name for t in threads if t.is_alive()] == []
        assert [t.name for t in _new_workers(before) if t.is_alive()] == []
    finally:
        os.chmod("isocenter.key", 0o600)


def test_a_directory_is_refused_as_valueerror_naming_the_path(tmp_path):
    _directory("isocenter.key")
    expected = os.path.abspath("isocenter.key")
    with pytest.raises(ValueError) as exc_info:
        with Session("s.db"):
            pass
    assert expected in str(exc_info.value)
    assert "is not a regular file" in str(exc_info.value)

    os.rmdir("isocenter.key")
    other = tmp_path / "keydir"
    other.mkdir()
    with Session("t.db") as session:
        with pytest.raises(ValueError) as exc_info:
            session.enable_reversible_anonymization(str(other))
        assert str(other) in str(exc_info.value)
        assert "is not a regular file" in str(exc_info.value)
        assert session.key_manager is None


def test_the_lock_refuses_a_directory_as_valueerror(tmp_path):
    """`_key_for_planning` catches only `FileNotFoundError`; a directory
    there now reaches the lock as the documented `ValueError`."""
    (tmp_path / "k").mkdir()
    with pytest.raises(ValueError, match="is not a regular file"):
        KeyManager(str(tmp_path / "k"))._key_for_planning()


def _child_env():
    return {k: v for k, v in os.environ.items()
            if not k.startswith("COVERAGE_")}


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFOs only")
def test_a_fifo_is_refused_not_waited_on():
    """In a subprocess: on the bug, `Session()` blocks in `open()` for
    good, which in-process would hang the suite."""
    os.mkfifo("isocenter.key")
    expected = os.path.abspath("isocenter.key")
    code = (
        "from isocenter import Session\n"
        "try:\n"
        "    with Session('s.db'):\n"
        "        print('NO RAISE')\n"
        "except ValueError as exc:\n"
        "    print('VALUEERROR', exc)\n")
    done = subprocess.run([sys.executable, "-c", code], cwd=os.getcwd(),
                          env=_child_env(), capture_output=True, text=True,
                          timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    assert "VALUEERROR" in done.stdout, done.stdout + done.stderr
    assert expected in done.stdout
    assert "is not a regular file" in done.stdout


def test_the_explicit_door_does_not_close_the_session():
    """A failed `enable_reversible_anonymization()` on an open session
    caches nothing and leaves the session usable; only construction
    closes what it started."""
    os.makedirs("input")
    with open(get_testdata_file("CT_small.dcm"), "rb") as src, \
            open(os.path.join("input", "ct.dcm"), "wb") as dst:
        dst.write(src.read())
    _malformed("bad.key")
    with Session("s.db") as session:
        with pytest.raises(ValueError) as exc_info:
            session.enable_reversible_anonymization("bad.key")
        assert os.path.abspath("bad.key") in str(exc_info.value)
        assert session.key_manager is None
        assert session.persistence_manager.thread.is_alive()
        assert session.store_backend._audit_thread.is_alive()
        summary = session.ingest("input")
        assert summary.ingested == 1


def test_a_failing_close_does_not_replace_the_cause(monkeypatch, caplog):
    _malformed("isocenter.key")
    original = PersistenceManager.shutdown
    managers = []

    def _sentinel(self):
        managers.append(self)
        raise RuntimeError("shutdown-sentinel")

    monkeypatch.setattr(PersistenceManager, "shutdown", _sentinel)
    try:
        with caplog.at_level(logging.ERROR, logger="isocenter"):
            with pytest.raises(ValueError) as exc_info:
                with Session("s.db"):
                    pass
        assert os.path.abspath("isocenter.key") in str(exc_info.value)
        assert any("shutdown-sentinel" in r.getMessage()
                   for r in caplog.records if r.levelno >= logging.ERROR), \
            [r.getMessage() for r in caplog.records]
    finally:
        for manager in managers:
            original(manager)


def test_a_valid_key_enables():
    _valid("isocenter.key")
    with Session("s.db") as session:
        assert session.key_manager.key_path == os.path.abspath("isocenter.key")
        assert session.reversibility_service is not None


def test_no_key_leaves_it_off():
    with Session("s.db") as session:
        assert session.key_manager is None
    assert not os.path.exists("isocenter.key")


def test_a_symlink_to_a_valid_key_enables(tmp_path):
    """The not-a-file check follows links: kills an `lstat`/`islink` form."""
    target = tmp_path / "real.key"
    _valid(str(target))
    os.symlink(str(target), "isocenter.key")
    with Session("s.db") as session:
        assert session.key_manager.key_path == os.path.abspath("isocenter.key")
        assert session.reversibility_service is not None


def test_an_empty_key_keeps_its_words():
    _empty("isocenter.key")
    with pytest.raises(ValueError, match="is empty, so it holds no key"):
        with Session("s.db"):
            pass


def test_only_the_implicit_door_says_where_it_found_the_key():
    _malformed("isocenter.key")
    with pytest.raises(ValueError) as exc_info:
        with Session("s.db"):
            pass
    assert "found it in the working directory" in str(exc_info.value)

    os.remove("isocenter.key")
    _malformed("bad.key")
    with Session("t.db") as session:
        with pytest.raises(ValueError) as exc_info:
            session.enable_reversible_anonymization("bad.key")
    assert "does not hold a Fernet key" in str(exc_info.value)
    assert "found it in the working directory" not in str(exc_info.value)


def test_the_key_file_mode_is_untouched_by_the_check():
    """Guard: the check reads the path's type and writes nothing."""
    _valid("isocenter.key")
    os.chmod("isocenter.key", 0o640)
    with Session("s.db") as session:
        assert session.key_manager is not None
    assert stat.S_IMODE(os.stat("isocenter.key").st_mode) == 0o640


def test_a_symlink_to_a_directory_is_refused_as_valueerror(tmp_path):
    """`isfile` follows the link, so a link to a directory is not a file."""
    target = tmp_path / "keydir"
    target.mkdir()
    os.symlink(str(target), "isocenter.key")
    with pytest.raises(ValueError) as exc_info:
        with Session("s.db"):
            pass
    assert os.path.abspath("isocenter.key") in str(exc_info.value)
    assert "is not a regular file" in str(exc_info.value)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFOs only")
def test_a_symlink_to_a_fifo_is_refused_not_waited_on(tmp_path):
    """In a subprocess, as for a FIFO itself: `open()` through the link
    would block for good."""
    os.mkfifo(str(tmp_path / "pipe"))
    os.symlink(str(tmp_path / "pipe"), "isocenter.key")
    expected = os.path.abspath("isocenter.key")
    code = (
        "from isocenter import Session\n"
        "try:\n"
        "    with Session('s.db'):\n"
        "        print('NO RAISE')\n"
        "except ValueError as exc:\n"
        "    print('VALUEERROR', exc)\n")
    done = subprocess.run([sys.executable, "-c", code], cwd=os.getcwd(),
                          env=_child_env(), capture_output=True, text=True,
                          timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    assert "VALUEERROR" in done.stdout, done.stdout + done.stderr
    assert expected in done.stdout
    assert "is not a regular file" in done.stdout

"""An empty or corrupt key file is not cached for the session, and no
reader ever sees a key file that is not yet written (#618).

Measured on 347ee93 (`probes-I/p_key.py empty_after_enable`,
`corrupt_after_enable`): a crash between the key file's `O_EXCL` create
and its write left an empty file, and every later session then raised
`RuntimeError: Key not loaded. Call load_key() or load_or_generate_key()
first.` from every lock form -- including after the file was filled,
because the empty read was cached as the session's key (`b''`) and
`load_or_generate_key` short-circuits on `key is not None`. A corrupt
file was cached the same way and repeated its `ValueError` whatever the
file then held.

Now `KeyManager.load_key` validates before it assigns `self.key` -- read
non-empty, and `Fernet(key)` builds -- so a second call after the file
is fixed succeeds, and an empty file gets its own `ValueError` naming
the path (Q7). The first lock creates the key as a temporary file in the
key's directory at 0600 and hard-links it into place, so no other
session ever reads a key file that is not yet written; on a filesystem
without hard links it falls back to the exclusive create E2 introduced.

Every message is checked for the Patient ID too (P6): a key message
names the key path, which is the caller's own argument, and nothing
else.
"""
import errno
import os
import stat
import subprocess
import sys
import textwrap

import pytest
from cryptography.fernet import Fernet

from isocenter import Session
from isocenter.crypto import KeyManager

from support.ct_small_files import write_ct

PID_A, PID_B = "PAT-618-A", "PAT-618-B"
FORMS = ("single", "batch", "report")


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _session(tmp_path, db="s.db"):
    write_ct(tmp_path / "in" / "a.dcm", PID_A, "6181", name="Secret^A")
    write_ct(tmp_path / "in" / "b.dcm", PID_B, "6182", name="Secret^B")
    session = Session(str(tmp_path / db))
    session.ingest(str(tmp_path / "in"))
    return session


def _first(session, pid=PID_A):
    patient = next(p for p in session.store.patients if p.patient_id == pid)
    return patient.studies[0].series[0].instances[0]


def _tokens(session):
    return sorted(p.patient_id for p in session.store.patients
                  if "0400,0500" in p.studies[0].series[0].instances[0].sequences)


def _lock(session, form):
    if form == "single":
        return session.lock_identities(PID_A)
    if form == "batch":
        return session.lock_identities([PID_A, PID_B])
    return session.lock_identities(session.audit())


def _assert_no_patient(message):
    for pid in (PID_A, PID_B, "Secret^A", "Secret^B"):
        assert pid not in message, message


@pytest.mark.parametrize("form", ("enable",) + FORMS)
def test_an_empty_key_file_raises_its_own_error(tmp_path, form):
    """T18. An empty file is named as empty, with the path, from every door;
    nothing is cached and no token is written. Kills the empty check
    dropped (M21: `Fernet(b'')` raises its own, path-less `ValueError`)
    and the cache before validation (M22)."""
    key = tmp_path / "k.key"
    with _session(tmp_path) as session:
        if form == "enable":
            key.write_bytes(b"")
            with pytest.raises(ValueError) as caught:
                session.enable_reversible_anonymization(str(key))
            assert session.reversibility_service is None
        else:
            session.enable_reversible_anonymization(str(key))
            key.write_bytes(b"")
            with pytest.raises(ValueError) as caught:
                _lock(session, form)
            assert session.key_manager.key is None
            assert _tokens(session) == []
        message = str(caught.value)
        assert str(key) in message and "empty" in message, message
        assert "Key not loaded" not in message, message
        _assert_no_patient(message)


@pytest.mark.parametrize("form", FORMS)
def test_a_key_file_filled_after_an_empty_read_locks_in_the_same_session(tmp_path, form):
    """T19. E2's race shape without the sleep: session B reads the file
    while it is empty, session A finishes writing it, and B's next lock
    succeeds under A's key. Kills M22."""
    key = tmp_path / "k.key"
    theirs = Fernet.generate_key()
    with _session(tmp_path) as session:
        session.enable_reversible_anonymization(str(key))
        key.write_bytes(b"")
        with pytest.raises(ValueError):
            _lock(session, form)
        assert session.key_manager.key is None
        key.write_bytes(theirs)
        _lock(session, form)
        assert session.key_manager.key == theirs
        assert PID_A in _tokens(session)
        session.recover_patient_identity(PID_A, restore=False)


def test_a_corrupt_key_file_is_not_cached(tmp_path):
    """T20. Garbage raises the key's own `ValueError` and is not cached;
    replaced by a valid key, the next lock succeeds. Kills M22."""
    key = tmp_path / "k.key"
    theirs = Fernet.generate_key()
    with _session(tmp_path) as session:
        session.enable_reversible_anonymization(str(key))
        key.write_bytes(b"not a fernet key at all")
        with pytest.raises(ValueError) as caught:
            session.lock_identities(PID_A)
        assert session.key_manager.key is None
        assert "not a fernet key" not in str(caught.value), "the file's bytes were echoed"
        _assert_no_patient(str(caught.value))
        key.write_bytes(theirs)
        session.lock_identities(PID_A)
        assert session.key_manager.key == theirs
        session.recover_patient_identity(PID_A, restore=False)


def test_load_key_validates_before_it_caches(tmp_path):
    """The `KeyManager` contract on its own: an empty file and a malformed
    one each raise `ValueError` and leave `key` None; a valid file is read
    once and then served from memory."""
    path = tmp_path / "k.key"
    manager = KeyManager(str(path))
    path.write_bytes(b"")
    with pytest.raises(ValueError, match="empty"):
        manager.load_key()
    assert manager.key is None
    path.write_bytes(b"garbage")
    with pytest.raises(ValueError):
        manager.load_key()
    assert manager.key is None
    theirs = Fernet.generate_key()
    path.write_bytes(theirs)
    assert manager.load_key() == theirs
    path.write_bytes(b"")
    assert manager.load_key() == theirs, "a loaded key is served from memory"


def test_no_reader_ever_sees_an_empty_key_file(tmp_path, monkeypatch):
    """T21. At the moment the key is linked into place it is already fully
    written at 0600, and nothing else is left in the directory afterwards.
    Kills the `O_EXCL` create followed by the write (M23)."""
    key = tmp_path / "keys" / "k.key"
    key.parent.mkdir()
    seen = []
    real_link = os.link

    def spy(src, dst, *args, **kwargs):
        seen.append((os.path.exists(dst), os.path.getsize(src),
                     stat.S_IMODE(os.stat(src).st_mode), os.path.dirname(src)))
        return real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr("isocenter.crypto.os.link", spy)
    manager = KeyManager(str(key))
    generated = manager.load_or_generate_key()
    assert seen == [(False, 44, 0o600, str(key.parent))], seen
    assert key.read_bytes() == generated and len(generated) == 44
    assert os.listdir(key.parent) == ["k.key"], "a temporary file was left behind"
    assert stat.S_IMODE(os.stat(key).st_mode) == 0o600


def test_the_first_lock_still_creates_the_key_at_0600(tmp_path):
    """T22. E2's P2 promise on the link route, under the most permissive
    umask there is: the temporary file is created 0600 and the link keeps
    the inode's mode. Kills the temporary file created at the umask's
    mode (M24)."""
    key = tmp_path / "k.key"
    previous = os.umask(0o000)
    try:
        with _session(tmp_path) as session:
            session.enable_reversible_anonymization(str(key))
            assert not key.exists()
            session.lock_identities(PID_A)
    finally:
        os.umask(previous)
    assert stat.S_IMODE(os.stat(key).st_mode) == 0o600
    assert len(key.read_bytes()) == 44
    assert not [name for name in os.listdir(tmp_path) if name.startswith("k.key.")], (
        "a temporary file was left behind")


def test_a_key_that_appears_while_the_link_is_attempted_is_loaded_not_replaced(
        tmp_path, monkeypatch):
    """The two-session race, forced: another session's key lands at the
    path between this session's generate and its link. `os.link` raises
    `FileExistsError`, the winner is loaded, this session's temporary file
    is removed, and the file at the path is the winner's. Kills the
    `FileExistsError` arm treated as any other `OSError` (falling back to
    the exclusive create, which would also see the winner -- so the assert
    that matters is the key served: the winner's, never the loser's)."""
    key = tmp_path / "k.key"
    theirs = Fernet.generate_key()
    mine = Fernet.generate_key()
    monkeypatch.setattr("isocenter.crypto.Fernet.generate_key",
                        staticmethod(lambda: mine))
    real_link = os.link

    def racing_link(src, dst, *args, **kwargs):
        key.write_bytes(theirs)
        return real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr("isocenter.crypto.os.link", racing_link)
    manager = KeyManager(str(key))
    assert manager.load_or_generate_key() == theirs
    assert manager.key == theirs
    assert key.read_bytes() == theirs
    assert os.listdir(tmp_path) == ["k.key"], "a temporary file was left behind"


def test_a_filesystem_without_hard_links_falls_back_to_the_exclusive_create(
        tmp_path, monkeypatch):
    """`os.link` raising any `OSError` but `FileExistsError` (EPERM on a
    filesystem without hard links) falls back to E2's exclusive create:
    the key is still written, at 0600, and no temporary file remains.
    Kills the fallback dropped (the lock would raise `OSError`)."""
    key = tmp_path / "k.key"

    def no_links(src, dst, *args, **kwargs):
        raise OSError(errno.EPERM, "Operation not permitted", src)

    monkeypatch.setattr("isocenter.crypto.os.link", no_links)
    previous = os.umask(0o000)
    try:
        manager = KeyManager(str(key))
        generated = manager.load_or_generate_key()
    finally:
        os.umask(previous)
    assert key.read_bytes() == generated and len(generated) == 44
    assert stat.S_IMODE(os.stat(key).st_mode) == 0o600
    assert os.listdir(tmp_path) == ["k.key"], "a temporary file was left behind"


def test_two_processes_creating_one_key_agree(tmp_path):
    """Four interpreters race to create one key path; every one of them
    ends holding the bytes the file holds, and the directory holds only
    the key. Not deterministic about who wins, and does not need to be:
    the property is that nobody serves a key the file does not."""
    key = tmp_path / "race" / "k.key"
    key.parent.mkdir()
    script = textwrap.dedent(f"""
        import sys
        from isocenter.crypto import KeyManager
        sys.stdout.write(KeyManager({str(key)!r}).load_or_generate_key().decode())
    """)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    procs = [subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=env) for _ in range(4)]
    outs = [p.communicate(timeout=120) for p in procs]
    for proc, (out, err) in zip(procs, outs):
        assert proc.returncode == 0, err.decode()
    keys = {out.decode() for out, _ in outs}
    assert keys == {key.read_text()}, keys
    assert os.listdir(key.parent) == ["k.key"]


def test_a_key_path_in_a_missing_directory_still_raises_at_the_first_lock(tmp_path):
    """E2 documented `FileNotFoundError` from the create for a missing
    parent directory; the temporary file is created in that directory, so
    the same exception, and nothing is written anywhere."""
    key = tmp_path / "nodir" / "k.key"
    with _session(tmp_path) as session:
        session.enable_reversible_anonymization(str(key))
        with pytest.raises(FileNotFoundError):
            session.lock_identities(PID_A)
        assert _tokens(session) == []
    assert not (tmp_path / "nodir").exists()

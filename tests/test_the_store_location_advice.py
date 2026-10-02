"""The session store belongs on local disk, and `Session()` says so when it
recognises a network filesystem under it (#839, owner ruling Q4 B).

The store opens SQLite in WAL mode, which SQLite documents as not working
over a network filesystem (https://sqlite.org/wal.html), and its locks are
`flock`, whose behaviour on NFS, SMB, Lustre or GPFS depends on the mount.
The docs say to keep the store local; `Session()` writes one WARNING log
line when the store's filesystem type is a known network type, and opens
anyway.

No test here needs a real mount. The detector is patched one level below
`persistence._network_filesystem_type`: at `persistence._filesystem_type`,
which that function looks up as a module global on every call, so the
patch reaches the classifier and the emit site in `Session()` both. The
readers under it are tested as pure functions over fixture text.
"""
import logging
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import types

import pytest
from pydicom.data import get_testdata_file

from isocenter import Session
from isocenter import persistence

_DOCS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(
    __file__))), "docs")


def _network_warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING and "network filesystem" in
            r.getMessage()]


def test_the_store_is_wal_and_the_quickstart_says_keep_it_local():
    """The advice rests on the journal mode. Whoever changes it comes back
    here, and to the docs that explain why the store is kept local:
    `docs/quickstart.md` ("Keep the store on local disk"),
    `docs/configuration.md`, `docs/environment.md`, `docs/architecture.md`
    and `Session.__init__`'s docstring."""
    with Session("s.db"):
        pass
    with sqlite3.connect("s.db") as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal", (
        "the store's journal mode moved off WAL; revisit the local-disk "
        "advice in docs/quickstart.md and the pages it names (#839)")
    with open(os.path.join(_DOCS, "quickstart.md"), encoding="utf-8") as fh:
        assert '!!! warning "Keep the store on local disk"' in fh.read()


def test_a_store_on_a_network_filesystem_draws_one_warning(monkeypatch, caplog):
    monkeypatch.setattr(persistence, "_filesystem_type", lambda path: "nfs")
    os.makedirs("input")
    shutil.copy(get_testdata_file("CT_small.dcm"), "input/ct.dcm")
    with caplog.at_level(logging.WARNING, logger="isocenter"):
        with Session("s.db") as session:
            warnings = _network_warnings(caplog)
            # The open is not refused: the session works.
            assert session.ingest("input").ingested == 1
    assert len(warnings) == 1, warnings
    assert os.path.dirname(os.path.abspath("s.db")) in warnings[0]
    assert "on a nfs filesystem" in warnings[0]
    assert "keep the store on a local disk" in warnings[0]
    assert "The session is opened anyway." in warnings[0]


def test_a_local_filesystem_draws_none(monkeypatch, caplog):
    monkeypatch.setattr(persistence, "_filesystem_type", lambda path: "apfs")
    with caplog.at_level(logging.WARNING, logger="isocenter"):
        with Session("s.db"):
            pass
    assert _network_warnings(caplog) == []


def test_a_memory_store_asks_about_its_pixel_file(monkeypatch, caplog):
    seen = []

    def _smb(path):
        seen.append(path)
        return "smbfs"

    monkeypatch.setattr(persistence, "_filesystem_type", _smb)
    with caplog.at_level(logging.WARNING, logger="isocenter"):
        with Session(":memory:") as session:
            sidecar_dir = os.path.dirname(
                os.path.realpath(session.store_backend.sidecar_path))
    warnings = _network_warnings(caplog)
    assert seen == [sidecar_dir]
    assert os.path.realpath(sidecar_dir) == os.path.realpath(
        tempfile.gettempdir())
    assert len(warnings) == 1, warnings
    assert "the session store's pixel file" in warnings[0]
    assert "on a smbfs filesystem" in warnings[0]


@pytest.mark.parametrize("fstype", sorted(persistence._NETWORK_FILESYSTEM_TYPES))
def test_each_known_network_type_is_recognised(monkeypatch, fstype):
    monkeypatch.setattr(persistence, "_filesystem_type", lambda path: fstype)
    assert persistence._network_filesystem_type("/x") == fstype


@pytest.mark.parametrize("fstype", [
    "apfs", "hfs", "ext4", "xfs", "btrfs", "zfs", "tmpfs", "overlay",
    "nfsd", "autofs", None, ""])
def test_a_local_or_unknown_type_is_not(monkeypatch, fstype):
    """Matched whole: `nfsd` (Linux's NFS-server control filesystem) is
    not NFS."""
    monkeypatch.setattr(persistence, "_filesystem_type", lambda path: fstype)
    assert persistence._network_filesystem_type("/x") is None


def test_an_unknown_platform_reads_no_type(monkeypatch):
    monkeypatch.setattr(persistence, "sys", types.SimpleNamespace(
        platform="sunos5"))
    assert persistence._filesystem_type(os.getcwd()) is None


def test_a_reader_that_raises_reads_no_type_and_the_open_proceeds(
        monkeypatch, caplog):
    def _boom(*args):
        raise OSError("reader-sentinel")

    monkeypatch.setattr(persistence, "_darwin_filesystem_type", _boom)
    monkeypatch.setattr(persistence, "_mountinfo_filesystem_type", _boom)
    assert persistence._filesystem_type(os.getcwd()) is None
    with caplog.at_level(logging.WARNING, logger="isocenter"):
        with Session("s.db"):
            pass
    assert _network_warnings(caplog) == []


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS statfs")
def test_darwin_refuses_a_reading_statvfs_disagrees_with(monkeypatch):
    """The ctypes read is believed only when two of its fields agree with
    `os.statvfs`; otherwise the struct layout is not the one assumed."""
    real = os.statvfs(os.getcwd())
    assert persistence._darwin_filesystem_type(os.getcwd())
    monkeypatch.setattr(persistence.os, "statvfs", lambda path: types.SimpleNamespace(
        f_fsid=real.f_fsid + 1, f_frsize=real.f_frsize))
    assert persistence._darwin_filesystem_type(os.getcwd()) is None


def test_the_real_detector_reads_this_directory_without_raising():
    fstype = persistence._filesystem_type(os.getcwd())
    assert fstype is None or (isinstance(fstype, str) and fstype)
    if sys.platform == "darwin" or sys.platform.startswith("linux"):
        assert fstype, "the detector read nothing on a platform it supports"


MOUNTINFO = r"""22 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw
30 22 0:40 / /mnt/nfs rw,relatime shared:5 - nfs4 srv:/export rw,vers=4.2
31 22 8:2 / /mnt/nfsdata rw,relatime - ext4 /dev/sdb1 rw
32 22 0:41 / /mnt/my\040share rw,relatime shared:7 master:3 - cifs //srv/s rw
33 30 0:42 / /mnt/nfs/scratch rw - tmpfs tmpfs rw
34 22 0:43 / /stack rw - ext4 /dev/sdc1 rw
35 22 0:44 / /stack rw - lustre 10.0.0.1@tcp:/fs rw
"""


@pytest.mark.parametrize("path,expected", [
    ("/mnt/nfs/a/b", "nfs4"),
    ("/mnt/nfs", "nfs4"),
    ("/mnt/nfsdata/x", "ext4"),
    # No mount at /mnt/nfsother: a string prefix would say nfs4.
    ("/mnt/nfsother/y", "ext4"),
    ("/mnt/my share/x", "cifs"),
    ("/mnt/nfs/scratch/x", "tmpfs"),
    ("/stack/x", "lustre"),
    ("/home/u", "ext4"),
])
def test_the_mountinfo_reader(path, expected):
    """Whole-component prefixes, the deepest mount, an escaped space,
    optional fields before the separator, and a later mount over an
    earlier one at the same point."""
    assert persistence._mountinfo_filesystem_type(MOUNTINFO, path) == expected


def test_the_mountinfo_reader_skips_a_malformed_line():
    text = "garbage\n22 1 8:1 / / rw - ext4 /dev/sda1 rw\n31 22 8:2 / /x rw -\n"
    assert persistence._mountinfo_filesystem_type(text, "/x/y") == "ext4"


def test_no_mount_holds_a_relative_path():
    assert persistence._mountinfo_filesystem_type(
        "30 22 0:40 / /mnt/nfs rw - nfs srv:/e rw\n", "rel/path") is None


def test_the_quickstart_names_what_may_be_on_network_storage():
    with open(os.path.join(_DOCS, "quickstart.md"), encoding="utf-8") as fh:
        text = fh.read()
    block = re.search(r'!!! warning "Keep the store on local disk"\n((?:    .*\n|\n)+)',
                      text)
    assert block, "the admonition has no indented body"
    body = " ".join(block.group(1).split())
    assert "WAL" in body and "flock" in body
    assert "export()" in body and "ingest()" in body


def test_the_warning_writes_no_audit_row(monkeypatch, caplog):
    """A log line, not a row: a row would grade every such run
    REVIEW_REQUIRED, and the ruling asked for a WARNING log line only."""
    monkeypatch.setattr(persistence, "_filesystem_type", lambda path: "nfs")
    with caplog.at_level(logging.WARNING, logger="isocenter"):
        with Session("s.db") as session:
            assert len(_network_warnings(caplog)) == 1
            session.store_backend.flush_audit_queue()
            with sqlite3.connect("s.db") as conn:
                rows = conn.execute(
                    "SELECT action_type, details FROM audit_log").fetchall()
    assert not [r for r in rows if "network filesystem" in (r[1] or "")], rows


def test_a_symlinked_database_on_a_network_filesystem_warns(monkeypatch, caplog):
    """The database is a link into a directory on NFS while its link (and
    the sidecar, named after the link) sit on local disk. SQLite opens the
    target, so the target's directory is the one asked about."""
    os.makedirs("remote")
    remote = os.path.realpath("remote")
    with Session(os.path.join("remote", "s.db")):
        pass
    os.symlink(os.path.join(remote, "s.db"), "link.db")
    monkeypatch.setattr(persistence, "_filesystem_type",
                        lambda path: "nfs" if os.path.realpath(path) == remote
                        else "apfs")
    with caplog.at_level(logging.WARNING, logger="isocenter"):
        with Session("link.db"):
            pass
    warnings = _network_warnings(caplog)
    assert len(warnings) == 1, warnings
    assert warnings[0].startswith(f"{remote} holds the session store"), warnings

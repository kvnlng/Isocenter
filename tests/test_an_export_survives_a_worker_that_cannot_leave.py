"""`export()` returns, and writes what it would have written, when a worker
of its recycling pool can leave neither by its sentinel nor by SIGTERM
(#860).

Export always runs on `run_parallel(maxtasksperchild=25)`, whose
`multiprocessing.Pool` exited through `terminate()`: SIGTERM to each worker,
then a join with no timeout. A worker that outlived SIGTERM held `export()`
for good after every file had been written. The exit now gives the workers
`_BROKEN_POOL_GRACE_S` and SIGKILLs the rest, with one WARNING log line and
no audit row: the export's data is complete, so its grade does not change
(owner ruling Q3 on #860).

The worker is made unable to leave by the initializer every worker runs,
`parallel.resolve_worker_initializer`, patched in the parent after ingest so
that only the export's pool gets it.
"""
import filecmp
import functools
import logging
import sqlite3

from isocenter import parallel
from isocenter.session import DicomSession

from support.ct_small_files import write_ct
from support.project_secret import FIXED_A, load_fixed_secret
from test_parallel_contract import (_RecordedPool, _cannot_leave,
                                    _exit_records, _gone, _run_bounded)


def _files(folder):
    return sorted(p.relative_to(folder) for p in folder.rglob("*")
                  if p.is_file())


def _row_counts(db):
    """`{action_type: count}` over the whole audit log."""
    with sqlite3.connect(db) as conn:
        return dict(conn.execute(
            "SELECT action_type, COUNT(*) FROM audit_log "
            "GROUP BY action_type").fetchall())


def _added(before, after):
    return {kind: after.get(kind, 0) - before.get(kind, 0)
            for kind in set(before) | set(after)
            if after.get(kind, 0) != before.get(kind, 0)}


def _rows(db, kinds):
    with sqlite3.connect(db) as conn:
        marks = ", ".join("?" for _ in kinds)
        return conn.execute(
            f"SELECT action_type, details FROM audit_log "
            f"WHERE action_type IN ({marks})", kinds).fetchall()


def test_an_export_returns_its_files_though_a_worker_cannot_leave(
        tmp_path, monkeypatch, caplog):
    """Three files, two workers that cannot leave: `export()` returns with
    all three written and no ERROR row, one WARNING names the kill, and the
    files and the audit rows it adds are those of a second export, from the
    same session, whose workers leave as usual: the files byte for byte,
    the rows kind for kind.

    Killing mutation: the kill loop deleted (the export never returns).
    """
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.setattr(parallel, "_BROKEN_POOL_GRACE_S", 1.0)
    monkeypatch.setattr(parallel, "_POOL_EXIT_AFTER_KILL_S", 30.0,
                        raising=False)
    started = []
    monkeypatch.setattr(_RecordedPool, "started", started)
    # The class `_run_on_recycling_pool` builds, by its module name (#887).
    monkeypatch.setattr(parallel, "_RecyclingPool", _RecordedPool)
    for index in range(3):
        write_ct(tmp_path / "in" / f"{index}.dcm", f"P860{index}",
                 f"860{index}")
    markers = tmp_path / "markers"
    markers.mkdir()
    db = str(tmp_path / "s.db")

    with DicomSession(db) as session:
        load_fixed_secret(session, tmp_path, FIXED_A)
        assert not session.ingest(str(tmp_path / "in")).failures
        session.save(sync=True)
        session.store_backend.flush_audit_queue()
        errors_before = _rows(db, ("ERROR",))
        counts_before = _row_counts(db)
        initializer = functools.partial(_cannot_leave, str(markers))
        with monkeypatch.context() as blocked:
            blocked.setattr(parallel, "resolve_worker_initializer",
                            lambda disable_gc=False: initializer)
            try:
                with caplog.at_level(logging.WARNING, logger="isocenter"):
                    outcome = _run_bounded(lambda: session.export(
                        str(tmp_path / "blocked"), use_compression=False),
                        120)
            finally:
                for process in list(started):
                    try:
                        process.kill()
                    except (OSError, ValueError):
                        pass
        assert not outcome["hung"], (
            "export() did not return: its recycling pool's exit joined a "
            "worker that cannot leave (#860)")
        assert "error" not in outcome, outcome
        summary = outcome["value"]
        assert summary.written == 3, summary.failures
        session.store_backend.flush_audit_queue()
        assert _rows(db, ("ERROR",)) == errors_before
        records = _exit_records(caplog)
        assert len(records) == 1, [r.getMessage() for r in caplog.records]
        assert "sent SIGKILL" in records[0].getMessage()
        # Its own pool's workers only: the session's ingest pool is alive.
        assert _gone(started), "a worker of the export's pool is still running"

        session.store_backend.flush_audit_queue()
        counts_blocked = _row_counts(db)
        again = session.export(str(tmp_path / "free"), use_compression=False)
        assert again.written == 3, again.failures
        session.store_backend.flush_audit_queue()
        counts_free = _row_counts(db)

    # Rows exactly once: the export whose workers were killed wrote the
    # same audit rows, kind for kind, as the one whose workers left. Not
    # vacuous: an export writes rows of its own.
    assert _added(counts_before, counts_blocked), counts_blocked
    assert _added(counts_before, counts_blocked) == _added(
        counts_blocked, counts_free), (counts_before, counts_blocked,
                                       counts_free)

    blocked_files = _files(tmp_path / "blocked")
    free_files = _files(tmp_path / "free")
    assert [p for p in blocked_files if p.suffix == ".dcm"], blocked_files
    assert blocked_files == free_files
    for relative in blocked_files:
        assert filecmp.cmp(tmp_path / "blocked" / relative,
                           tmp_path / "free" / relative, shallow=False), (
            relative)

"""`export()` returns, and says what it lost, when a worker of its recycling
pool is killed while it writes (#887).

Export always runs on `run_parallel(maxtasksperchild=25)`, a
`multiprocessing.Pool` that answered a dead worker by starting another and
waiting for the dead one's task for good: the out-of-memory killer on one
decoder held `export()` forever, before the pool's exit, where #860's bound
never ran. The pool now watches every worker it starts, and a death fails
the stream with `BrokenProcessPool`, which the export reports through its
existing path (owner ruling Q2 on #887): one `ERROR` row, `Export worker
failed: BrokenProcessPool: ...`, so the run grades `REVIEW_REQUIRED`, and
`ExportError` only if nothing was written.

The worker is killed by the initializer every worker runs,
`parallel.resolve_worker_initializer`, patched in the parent after ingest so
that only the export's pool gets it. The initializer replaces
`io_handlers._export_instance_worker` in the child; the task pickles that
function by reference, so the child resolves the replacement.
"""
import functools
import os
import signal
import sqlite3

from isocenter import parallel
from isocenter.io_handlers import ExportError
from isocenter.session import DicomSession

from support.ct_small_files import write_ct
from support.project_secret import FIXED_A, load_fixed_secret
from test_parallel_contract import _RecordedPool, _gone, _run_bounded


def _kill_the_worker_writing(uid):
    """Pool initializer: the worker that is handed instance `uid` SIGKILLs
    itself before it writes, as the out-of-memory killer would. Module
    scope: it pickles into the worker."""
    # pylint: disable=import-outside-toplevel
    from isocenter import io_handlers
    real = io_handlers._export_instance_worker

    def killed_on_uid(ctx):
        if getattr(ctx.instance, "sop_instance_uid", None) == uid:
            os.kill(os.getpid(), signal.SIGKILL)
        return real(ctx)

    io_handlers._export_instance_worker = killed_on_uid


def _error_rows(db):
    with sqlite3.connect(db) as conn:
        return [details for (details,) in conn.execute(
            "SELECT details FROM audit_log WHERE action_type = 'ERROR'")]


def test_an_export_whose_worker_is_killed_returns_and_reports_it(
        tmp_path, monkeypatch):
    """Three files, two workers, and the worker handed one chosen instance
    is SIGKILLed: `export()` returns (or raises `ExportError` when nothing
    was written) within the bound, one `ERROR` row names the
    `BrokenProcessPool`, the chosen instance has no file, and no worker of
    the export's pool is left.

    Killing mutation: the stream's watch removed (the export never returns).
    """
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.setattr(parallel, "_POOL_WATCH_S", 0.2, raising=False)
    started = []
    monkeypatch.setattr(_RecordedPool, "started", started)
    monkeypatch.setattr(parallel, "_RecyclingPool", _RecordedPool)
    for index in range(3):
        write_ct(tmp_path / "in" / f"{index}.dcm", f"P887{index}",
                 f"887{index}")
    db = str(tmp_path / "s.db")
    out = tmp_path / "out"

    with DicomSession(db) as session:
        load_fixed_secret(session, tmp_path, FIXED_A)
        assert not session.ingest(str(tmp_path / "in")).failures
        session.save(sync=True)
        session.store_backend.flush_audit_queue()
        uids = sorted(instance.sop_instance_uid
                      for patient in session.store.patients
                      for study in patient.studies
                      for series in study.series
                      for instance in series.instances)
        assert len(uids) == 3, uids
        chosen = uids[1]
        errors_before = _error_rows(db)
        initializer = functools.partial(_kill_the_worker_writing, chosen)
        with monkeypatch.context() as killing:
            killing.setattr(parallel, "resolve_worker_initializer",
                            lambda disable_gc=False: initializer)
            try:
                outcome = _run_bounded(lambda: session.export(
                    str(out), use_compression=False), 120)
            finally:
                for process in list(started):
                    try:
                        process.kill()
                    except (OSError, ValueError):
                        pass
        assert not outcome["hung"], (
            "export() did not return: its recycling pool waited for good on "
            "the task a killed worker held (#887)")
        if "error" in outcome:
            assert isinstance(outcome["error"], ExportError), outcome
        else:
            assert outcome["value"].written < 3, outcome["value"]
        session.store_backend.flush_audit_queue()
        added = _error_rows(db)[len(errors_before):]

    broken = [row for row in added
              if row.startswith("Export worker failed: BrokenProcessPool: ")]
    assert len(broken) == 1, added
    assert "exit code -9" in broken[0], broken[0]
    written = [p.name for p in out.rglob("*") if p.is_file()]
    assert not [name for name in written if chosen in name], written
    assert _gone(started), "a worker of the export's pool is still running"

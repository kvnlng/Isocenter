"""A worker `run_parallel` loses is a reported failure, not a vanished pass.

Both consumers of a parallel result carry an `isinstance(result,
Exception)` arm, written for "`run_parallel` handing back a worker that
died" -- and `run_parallel` never did that: every strategy re-raised a
worker's exception at the point of iteration, so the arm never ran, the
results still queued behind the raise were discarded with it, no `ERROR`
row was written for any of them, and the caller got a bare
`BrokenProcessPool` (or the worker's own exception) instead of the
`RedactionError` / `RuntimeError` accounting the arms exist to produce
(#232).

`run_parallel(..., yield_exceptions=True)` is what makes those arms
reachable; these tests pin the consumer-visible half: what a lost worker
looks like to `session.redact()`, `DicomExporter.write_tree()` and
`DicomExporter.export_batch()`.

`ISOCENTER_FORCE_THREADS` throughout, and it is load-bearing twice over:
a monkeypatched worker cannot reach a separate process, and
`_resolve_strategy` reads the variable in the parent. The strategy-level
guarantee that the same contract holds on processes -- including a
worker killed outright -- is pinned in `test_parallel_contract.py`,
where the worker is importable.
"""
import sqlite3

import numpy as np
import pytest

import isocenter.io_handlers as io_handlers
import isocenter.services as services
from isocenter.entities import Equipment, Instance, Patient, Series, Study
from isocenter.io_handlers import DicomExporter
from isocenter.services import RedactionError
from isocenter.session import DicomSession

SN_OK = "SERIAL_OK"
SN_BAD = "SERIAL_BAD"
CT_STORAGE = "1.2.840.10008.5.1.4.1.1.2"
GOOD_ZONE = [0, 8, 0, 8]


def _series(serial, uids):
    series = Series(f"SE_{serial}", "OT", 1)
    series.equipment = Equipment("Acme", "Scanner", serial)
    for n, uid in enumerate(uids):
        inst = Instance(uid, CT_STORAGE, n + 1)
        inst.file_path = None
        inst.set_pixel_data(np.full((32, 32), 200, dtype=np.uint8))
        series.instances.append(inst)
    return series


def _redaction_session(tmp_path, name):
    """Two machines, one instance each, both with an applicable zone."""
    session = DicomSession(str(tmp_path / f"{name}.db"))
    patient = Patient("P1", "Test^Patient")
    study = Study("ST_1", "20230101")
    study.series.append(_series(SN_OK, ["1.2.3.ok.1"]))
    study.series.append(_series(SN_BAD, ["1.2.3.bad.1"]))
    patient.studies.append(study)
    session.store.patients.append(patient)
    session.configuration.rules = [
        {"serial_number": SN_OK, "redaction_zones": [GOOD_ZONE]},
        {"serial_number": SN_BAD, "redaction_zones": [GOOD_ZONE]},
    ]
    return session


def _audit_errors(db_path):
    """Straight out of sqlite after close -- not `get_audit_errors()` (#218)."""
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT entity_uid, details FROM audit_log "
            "WHERE action_type='ERROR'").fetchall()


def test_a_lost_redaction_worker_is_a_redaction_failure(tmp_path, monkeypatch):
    """`redact()` answers a lost worker with `RedactionError`, not the raw raise.

    Before #232 the `RuntimeError` below came straight out of
    `session.redact()`: the mutation queued behind it was discarded
    unapplied, no audit row was written for anything, and `.failures` /
    `.attempted` never existed. The arm in `_apply_redaction_outcomes`
    describes exactly this case and could never run.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    session = _redaction_session(tmp_path, "lostworker")
    db_path = session.store_backend.db_path
    insts = {i.sop_instance_uid: i
             for p in session.store.patients
             for st in p.studies
             for se in st.series
             for i in se.instances}

    real = services.RedactionService.execute_redaction_task

    def exploding(self, task):
        if task['machine_sn'] == SN_BAD:
            raise RuntimeError("worker exploded before it could answer")
        return real(self, task)

    monkeypatch.setattr(services.RedactionService, "execute_redaction_task",
                        exploding)

    try:
        with pytest.raises(RedactionError) as excinfo:
            session.redact(show_progress=False)

        assert excinfo.value.attempted == 2
        assert len(excinfo.value.failures) == 1, excinfo.value.failures
        uid, detail = excinfo.value.failures[0]
        assert uid == "UNKNOWN", (
            "a worker lost before it could answer has no outcome to name "
            "the instance with; the row still has to exist")
        assert "Redaction worker failed" in detail, detail
        assert "worker exploded" in detail, detail

        # The mutation queued behind the lost worker still landed.
        ok = insts["1.2.3.ok.1"]
        assert ok.attributes.get("_ISOCENTER_REDACTION_HASH"), (
            "the successful redaction was discarded along with the lost "
            "worker -- the exact loss #232 is about")
        assert ok.get_pixel_data()[0:8, 0:8].sum() == 0
    finally:
        session.close()

    rows = _audit_errors(db_path)
    assert len(rows) == 1, rows
    assert rows[0][0] == "UNKNOWN"
    assert "Redaction worker failed" in rows[0][1], rows


def _instance(uid):
    """Minimal but writeable: SC Storage, so the IOD validator asks nothing,
    with just enough pixels that the worker has something to write."""
    inst = Instance(uid, "1.2.840.10008.5.1.4.1.1.7", 1)
    inst.file_path = None
    inst.set_pixel_data(np.full((8, 8), 7, dtype=np.uint8))
    return inst


def _export_patient():
    patient = Patient("P1", "Test^Patient")
    study = Study("ST_1", "20230101")
    series = Series("SE_1", "OT", 1)
    series.instances.append(_instance("1.2.3.1"))
    series.instances.append(_instance("1.2.3.999"))
    study.series.append(series)
    patient.studies.append(study)
    return patient


def _patch_exploding_export_worker(monkeypatch):
    real = io_handlers._export_instance_worker

    def exploding(ctx):
        if ctx.instance.sop_instance_uid == "1.2.3.999":
            raise RuntimeError("export worker exploded")
        return real(ctx)

    monkeypatch.setattr(io_handlers, "_export_instance_worker", exploding)


def test_a_lost_export_worker_is_an_export_failure_in_write_tree(
        tmp_path, monkeypatch):
    """`write_tree` raises its own documented `RuntimeError`, with a count.

    Before #232 the worker's raise propagated bare out of the results
    loop: no success count, no failure count, and -- had the results
    behind it existed -- their loss went unreported.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    _patch_exploding_export_worker(monkeypatch)
    out = tmp_path / "out_tree"

    with pytest.raises(RuntimeError, match="Export incomplete. 1 failed"):
        DicomExporter.write_tree(_export_patient(), str(out),
                                 show_progress=False)

    written = sorted(p.name for p in out.rglob("*.dcm"))
    assert written == ["1.2.3.1.dcm"], (
        "the instance that exported cleanly did not survive the lost "
        f"worker beside it: {written}")


def test_a_lost_export_worker_is_accounted_by_export_batch(
        tmp_path, monkeypatch):
    """`export_batch` does not raise; the loss lands in the summary instead.

    Its contract since #181 is that the summary, not an exception, says
    what reached disk -- so a lost worker has to become a `failures` row
    (`_report_export_failures`' Exception arm), not a raise that takes
    the whole accounting with it.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    _patch_exploding_export_worker(monkeypatch)
    out = tmp_path / "out_batch"
    patient = _export_patient()
    tasks = DicomExporter._generate_export_contexts(
        patient, patient.studies, str(out), None)
    assert len(tasks) == 2, "the fixture stopped producing two contexts"

    summary = DicomExporter.export_batch(tasks, show_progress=False,
                                         total=len(tasks))

    assert summary.written_uids == ["1.2.3.1"], summary.written_uids
    assert len(summary.failures) == 1, summary.failures
    uid, detail = summary.failures[0]
    assert uid == "UNKNOWN", (
        "a worker lost before it could answer has no outcome to name the "
        "instance with; the row still has to exist")
    assert "Export worker failed" in detail, detail
    assert "export worker exploded" in detail, detail

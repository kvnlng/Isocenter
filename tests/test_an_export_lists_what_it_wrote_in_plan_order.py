"""`ExportSummary.written_uids` and `failures` are in plan order (#999).

Plan order is the store's patient, study, series and instance order, the
order of the output tree. Before #999 both lists were in the order the
export workers finished: `export_batch` reads `run_parallel`'s results
from `imap_unordered`. Measured on `main` at a946a1e6 over 12 instances of
mixed size, four exports on each of 3.12.14 and 3.14.7t: no export listed
`written_uids` in plan order, and the order changed between exports on
one interpreter.

What the owner ruled (2026-11-20):

- Q1: store (plan) order, sorted in the parent by a plan position. Not
  `ordered=True` on the pool: on that path the results held behind a
  dead worker's index are lost (#887).
- Q2: `failures`, `ExportError`'s `First:` and the per-instance audit rows
  follow the same order. A failure with no instance (a worker that died
  before it answered) comes after every named one. A #1020 collision loser
  is placed at its own plan position, not appended.

Completion order cannot be forced through spawned workers, so most tests
here replace `io_handlers.run_parallel` with a wrapper that calls the real
function and hands its results back **reversed**: the export still runs
real workers, and only the arrival order is the adversary's. Every
assertion compares a list, never a set.

This file names `isocenter.session` and `isocenter.io_handlers`, so both
probe rows are charged with it.
"""
import os
import sqlite3
from concurrent.futures.process import BrokenProcessPool

import numpy as np
import pydicom
import pytest
from pydicom.data import get_testdata_file

from isocenter import io_handlers
from isocenter import session as session_module
from isocenter.io_handlers import ExportContext, ExportError, ExportOutcome
from isocenter.session import DicomSession

ROOT = "1.2.826.0.1.3680043.10.9999.999"


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.setenv("ISOCENTER_SHOW_PROGRESS", "0")
    monkeypatch.delenv("ISOCENTER_FORCE_THREADS", raising=False)
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _write(path, number, big=False):
    """One CT of one fixed patient, study and series. `big` gives it a
    512x512 frame, so its write takes longer than a 128x128 one's."""
    ds = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    ds.PatientID = "PAT-999"
    ds.StudyInstanceUID = f"{ROOT}.1"
    ds.SeriesInstanceUID = f"{ROOT}.1.1"
    ds.SOPInstanceUID = f"{ROOT}.1.1.{number}"
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.InstanceNumber = number
    if big:
        frame = (np.arange(512 * 512, dtype=np.uint32) % 4000).astype(
            np.int16).reshape(512, 512)
        ds.Rows, ds.Columns = 512, 512
        ds.PixelData = frame.tobytes()
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    ds.save_as(str(path))


def _session(tmp_path, count, big=()):
    src = tmp_path / "in"
    for number in range(1, count + 1):
        _write(src / f"{number}.dcm", number, big=number in big)
    session = DicomSession(str(tmp_path / "s.db"))
    session.ingest(str(src))
    return session


def _plan(session):
    """The store's walk order, which is the export's plan order."""
    return [instance for patient in session.store.patients
            for study in patient.studies
            for series in study.series
            for instance in series.instances]


def _reversed_arrival(monkeypatch, front=()):
    """Every export batch's results handed back reversed, with `front`
    first. Ingest goes through the same `run_parallel` and is left alone."""
    real = io_handlers.run_parallel

    def reversing(func, *args, **kwargs):
        results = real(func, *args, **kwargs)
        if func is not io_handlers._export_instance_worker:
            return results
        return list(front) + list(results)[::-1]

    monkeypatch.setattr(io_handlers, "run_parallel", reversing)


def _rows(db_path, action="ERROR"):
    """Read after the session is closed, so the audit writer has drained."""
    with sqlite3.connect(str(db_path)) as conn:
        return [uid for (uid,) in conn.execute(
            "SELECT entity_uid FROM audit_log WHERE action_type = ? "
            "ORDER BY rowid", (action,))]


def _refuse(instance, tag):
    """A UID `export_file_name` refuses: it begins with a dot."""
    instance.sop_instance_uid = f".bad{tag}"
    return instance.sop_instance_uid


def test_written_uids_follow_the_plan_whatever_finishes_first(
        tmp_path, monkeypatch):
    _reversed_arrival(monkeypatch)
    with _session(tmp_path, 8) as session:
        planned = [i.sop_instance_uid for i in _plan(session)]
        summary = session.export(str(tmp_path / "out"),
                                 use_compression=False, show_progress=False)
    assert summary.written_uids == planned


def test_failures_follow_the_plan(tmp_path, monkeypatch):
    _reversed_arrival(monkeypatch)
    with _session(tmp_path, 8) as session:
        plan = _plan(session)
        bad = [_refuse(instance, n) for n, instance in enumerate(plan)
               if n % 2]
        good = [i.sop_instance_uid for i in plan
                if not i.sop_instance_uid.startswith(".bad")]
        summary = session.export(str(tmp_path / "out"),
                                 use_compression=False, show_progress=False)
    assert [uid for uid, _ in summary.failures] == bad
    assert summary.written_uids == good


def test_export_error_names_the_first_planned_failure(tmp_path, monkeypatch):
    _reversed_arrival(monkeypatch)
    with _session(tmp_path, 4) as session:
        bad = [_refuse(instance, n) for n, instance in
               enumerate(_plan(session))]
        with pytest.raises(ExportError) as raised:
            session.export(str(tmp_path / "out"), use_compression=False,
                           show_progress=False)
    assert [uid for uid, _ in raised.value.failures] == bad
    assert f"First: {bad[0]}:" in str(raised.value)


def test_the_per_instance_rows_are_written_in_plan_order(
        tmp_path, monkeypatch):
    _reversed_arrival(monkeypatch)
    with _session(tmp_path, 6) as session:
        bad = [_refuse(instance, n) for n, instance in
               enumerate(_plan(session)) if n != 2]
        session.export(str(tmp_path / "out"), use_compression=False,
                       show_progress=False)
    assert _rows(tmp_path / "s.db") == bad


def _collide(monkeypatch, first, second):
    """Make the volume answer that two delivered paths are one file."""
    real = session_module._delivered_file_identity

    def identity(path):
        name = os.path.basename(path)
        if name in (f"{first}.dcm", f"{second}.dcm"):
            return ("one", "file")
        return real(path)

    monkeypatch.setattr(session_module, "_delivered_file_identity", identity)


def test_a_collision_loser_takes_its_planned_place_in_failures(
        tmp_path, monkeypatch):
    """Instances 0 and 1 resolve to one file; 3 is refused. The loser of
    the pair is planned before 3, so it is listed before 3. Appended, it
    would come after."""
    _reversed_arrival(monkeypatch)
    with _session(tmp_path, 4) as session:
        plan = _plan(session)
        a, b = plan[0].sop_instance_uid, plan[1].sop_instance_uid
        late = _refuse(plan[3], 3)
        _collide(monkeypatch, a, b)
        summary = session.export(str(tmp_path / "out"),
                                 use_compression=False, show_progress=False)
    [survivor] = [uid for uid in summary.written_uids if uid in (a, b)]
    loser = b if survivor == a else a
    assert [uid for uid, _ in summary.failures] == [loser, late]
    assert summary.written_uids == [survivor, plan[2].sop_instance_uid]


def test_a_failure_with_no_instance_comes_last(tmp_path, monkeypatch):
    died = BrokenProcessPool("a worker died before it answered")
    _reversed_arrival(monkeypatch, front=[died])
    with _session(tmp_path, 4) as session:
        plan = _plan(session)
        bad = _refuse(plan[1], 1)
        summary = session.export(str(tmp_path / "out"),
                                 use_compression=False, show_progress=False)
    assert [uid for uid, _ in summary.failures] == [bad, "UNKNOWN"]


def test_an_instance_with_an_empty_uid_fails_at_its_planned_place(
        tmp_path, monkeypatch):
    """The second of five has UID `''`: it fails keyed UNKNOWN, but it has
    a plan position. The third and fourth resolve to one file, so the
    collision insert runs in the same export, and the fifth is refused.
    Ordered by UID, `''` would sort to an end; appended, the loser would
    follow the fifth."""
    _reversed_arrival(monkeypatch)
    with _session(tmp_path, 5) as session:
        plan = _plan(session)
        plan[1].sop_instance_uid = ""
        c, d = plan[2].sop_instance_uid, plan[3].sop_instance_uid
        late = _refuse(plan[4], 4)
        _collide(monkeypatch, c, d)
        summary = session.export(str(tmp_path / "out"),
                                 use_compression=False, show_progress=False)
    [survivor] = [uid for uid in summary.written_uids if uid in (c, d)]
    loser = d if survivor == c else c
    assert [uid for uid, _ in summary.failures] == ["UNKNOWN", loser, late]
    assert summary.written_uids == [plan[0].sop_instance_uid, survivor]


def test_two_instances_sharing_a_uid_keep_both_entries_in_plan_order(
        tmp_path, monkeypatch):
    """The sort must not de-duplicate: `written` counts distinct UIDs,
    the list does not. Four, so the reversed list is not the list."""
    _reversed_arrival(monkeypatch)
    with _session(tmp_path, 4) as session:
        plan = _plan(session)
        plan[3].sop_instance_uid = plan[0].sop_instance_uid
        uids = [i.sop_instance_uid for i in plan]
        summary = session.export(str(tmp_path / "out"),
                                 use_compression=False, show_progress=False)
    assert summary.written_uids == uids
    assert summary.written == 3


def test_an_unpatched_export_of_twelve_matches_the_plan(tmp_path):
    """No patch: every third instance is 512x512 and the rest small, as
    the measurement on #999 was. The end-to-end witness."""
    with _session(tmp_path, 12, big={1, 4, 7, 10}) as session:
        planned = [i.sop_instance_uid for i in _plan(session)]
        for _ in range(2):
            summary = session.export(str(tmp_path / "out"),
                                     use_compression=False,
                                     show_progress=False)
            assert summary.written_uids == planned


def test_every_outcome_carries_its_context_position(tmp_path):
    """The worker copies `ctx.position` into each of its three returns: a
    write, a refusal raised first, and a failure in the write."""
    with _session(tmp_path, 1) as session:
        [instance] = _plan(session)
        out = tmp_path / "out"
        good = ExportContext(
            instance=instance, output_path=str(out / "a.dcm"),
            patient_attributes={}, study_attributes={},
            series_attributes={}, position=7)
        refused = ExportContext(
            instance=instance, output_path="", patient_attributes={},
            study_attributes={}, series_attributes={},
            refusal=ValueError("refused"), position=8)
        # A directory where the file must go: the write itself fails.
        (out / "c.dcm").mkdir(parents=True)
        failing = ExportContext(
            instance=instance, output_path=str(out / "c.dcm"),
            patient_attributes={}, study_attributes={},
            series_attributes={}, position=9)
        outcomes = [io_handlers._export_instance_worker(ctx)
                    for ctx in (good, refused, failing)]
    assert [o.ok for o in outcomes] == [True, False, False]
    assert all(isinstance(o, ExportOutcome) for o in outcomes)
    assert [o.position for o in outcomes] == [7, 8, 9]

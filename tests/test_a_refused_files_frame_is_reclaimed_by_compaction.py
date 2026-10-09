"""A file refused after its pixels were read may leave them in the sidecar
until `compact()` (#943). These tests pin what `compact()` does about it;
they change nothing and are green on `main`.

`ingest()` appends a file's pixel or waveform frame to the sidecar in the
parent and then links the instance into the graph. A failure between the
two refuses the file -- an `ERROR` row, an entry in
`IngestSummary.failures`, a grade that is not `PASS` -- with its frame
already appended. Measured on `main` at fd359eb3 (twelve source files with
no injection): **no property of a file reaches that arm** since #747 moved
the multi-valued-key refusal into the worker; what can still raise there
is a sidecar or sqlite failure. So the failure here is injected, as
`tests/test_ingest_failure_audit.py` injects it: `isocenter.io_handlers`'s
`Patient` is made to raise for one Patient ID.

What is pinned, each by a mutant rather than by a red-then-green run:

1. the refused file's frame is dead space, and `compact()` reclaims it
   while the good file's pixels read back as they were;
2. a refused *waveform* file also leaves an `instance_blobs` row naming an
   instance the store does not hold (the waveform sites commit the row
   before the linkage), and `compact()` deletes the row with the bytes;
3. the refusal does not hold the file's UID: the same file ingested again
   is kept.

Not pinned, on purpose: a store with **nothing** live. `compact()` returns
early there and the refused frame stays; a test would freeze that.
Reordering ingest to link before it appends is not done here either (it
belongs with an interrupted ingest, in 1.1).
"""
import os
import sqlite3

import numpy as np
import pydicom
import pytest
from pydicom.data import get_testdata_file

from isocenter import Session
from isocenter import io_handlers

from support.ct_small_files import write_ct

BAD = "BAD-943"


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _refuse_the_linkage_of(monkeypatch, patient_id):
    """Make the parent's linkage raise for one Patient ID, after the
    file's frame has been appended. The worker is not touched."""
    real = io_handlers.Patient

    def patient(*args, **kwargs):
        if args and args[0] == patient_id:
            raise KeyError("injected")
        return real(*args, **kwargs)

    monkeypatch.setattr(io_handlers, "Patient", patient)


def _sidecar(tmp_path):
    return os.path.getsize(str(tmp_path / "s_pixels.bin"))


def _blobs(tmp_path):
    """`(kind, length, whether the store holds its instance)` per blob row."""
    with sqlite3.connect(str(tmp_path / "s.db")) as conn:
        return conn.execute(
            "SELECT b.kind, b.length, EXISTS (SELECT 1 FROM instances i "
            "WHERE i.sop_instance_uid = b.instance_uid) "
            "FROM instance_blobs b ORDER BY b.offset").fetchall()


def _good_instance(session):
    [instance] = [i for p in session.store.patients for st in p.studies
                  for se in st.series for i in se.instances]
    return instance


def test_a_refused_pixel_files_frame_is_reclaimed_and_the_good_file_reads_back(
        tmp_path, monkeypatch):
    write_ct(tmp_path / "in" / "a.dcm", BAD, 943)
    good = write_ct(tmp_path / "in" / "b.dcm", "GOOD-943", 944)
    pixels = pydicom.dcmread(good).pixel_array
    _refuse_the_linkage_of(monkeypatch, BAD)
    with Session(str(tmp_path / "s.db")) as session:
        summary = session.ingest(str(tmp_path / "in"))
        assert summary.ingested == 1
        [(path, reason)] = summary.failures
        assert os.path.basename(path) == "a.dcm"
        assert reason.startswith("Linkage Failed")
        session.save(sync=True)
        [(kind, frame, held)] = _blobs(tmp_path)
        assert (kind, held) == ("pixels", 1)
        # Two frames of the same pixels: the good file's, which the one
        # blob row names, and the refused file's, which nothing names.
        assert _sidecar(tmp_path) == 2 * frame
        session.compact()
        assert _sidecar(tmp_path) == frame
        assert _blobs(tmp_path) == [("pixels", frame, 1)]
        instance = _good_instance(session)
        instance.unload_pixel_data()
        assert np.array_equal(instance.get_pixel_data(), pixels)
    with Session(str(tmp_path / "s.db")) as reopened:
        assert np.array_equal(_good_instance(reopened).get_pixel_data(), pixels)


def test_a_refused_waveform_files_blob_row_goes_with_its_bytes(
        tmp_path, monkeypatch):
    os.makedirs(tmp_path / "in")
    ds = pydicom.dcmread(get_testdata_file("waveform_ecg.dcm"))
    ds.PatientID = BAD
    ds.save_as(str(tmp_path / "in" / "w.dcm"))
    write_ct(tmp_path / "in" / "b.dcm", "GOOD-943", 944)
    _refuse_the_linkage_of(monkeypatch, BAD)
    with Session(str(tmp_path / "s.db")) as session:
        summary = session.ingest(str(tmp_path / "in"))
        assert summary.ingested == 1
        assert [os.path.basename(path) for path, _ in summary.failures] == ["w.dcm"]
        session.save(sync=True)
        blobs = _blobs(tmp_path)
        [(_, frame, _)] = [row for row in blobs if row[0] == "pixels"]
        [(_, waveform, held)] = [row for row in blobs if row[0] != "pixels"]
        assert held == 0, "setup: the row names an instance the store does not hold"
        assert _sidecar(tmp_path) == frame + waveform
        session.compact()
        assert _blobs(tmp_path) == [("pixels", frame, 1)]
        assert _sidecar(tmp_path) == frame


def test_the_refused_file_ingested_again_is_kept(tmp_path, monkeypatch):
    write_ct(tmp_path / "in" / "a.dcm", BAD, 943)
    write_ct(tmp_path / "in" / "b.dcm", "GOOD-943", 944)
    with Session(str(tmp_path / "s.db")) as session:
        # A context of its own: `monkeypatch.undo()` would also undo the
        # autouse thread setting, and the second ingest would run under
        # the default pool.
        with monkeypatch.context() as patch:
            _refuse_the_linkage_of(patch, BAD)
            assert session.ingest(str(tmp_path / "in")).ingested == 1
        assert os.environ["ISOCENTER_FORCE_THREADS"] == "1"
        again = session.ingest(str(tmp_path / "in"))
        assert again.failures == []
        assert again.ingested == 1, "the refused file, and not the one already held"
        assert sorted(p.patient_id for p in session.store.patients) == \
            sorted([BAD, "GOOD-943"])
        session.save(sync=True)
        [frame] = {length for _, length, _ in _blobs(tmp_path)}
        assert _sidecar(tmp_path) == 3 * frame
        session.compact()
        assert _sidecar(tmp_path) == 2 * frame
        assert _blobs(tmp_path) == [("pixels", frame, 1)] * 2


def test_a_refused_file_does_not_hold_its_uid_within_the_same_call(
        tmp_path, monkeypatch):
    """Two files of one SOP Instance UID in **one** `ingest()` call, the
    first refused at linkage: the second is kept, not declined as a
    duplicate, because the refusal comes before the UID is recorded as
    held.

    The test above cannot see that: `held` is rebuilt from the graph at
    every call, so a second call never sees what the first call's refusal
    did to it. With `held[inst.sop_instance_uid] = inst` moved above the
    linkage, the test above stays green and this one is red (the second
    file is declined, `ingested == 0`)."""
    write_ct(tmp_path / "in" / "a.dcm", BAD, 943)
    write_ct(tmp_path / "in" / "b.dcm", "GOOD-943", 943)
    _refuse_the_linkage_of(monkeypatch, BAD)
    with Session(str(tmp_path / "s.db")) as session:
        summary = session.ingest(str(tmp_path / "in"))
        assert [os.path.basename(path) for path, _ in summary.failures] == ["a.dcm"]
        assert (summary.ingested, summary.declined) == (1, 0)
        assert [p.patient_id for p in session.store.patients] == ["GOOD-943"]

"""A failed write must not leave a file under the real name (#199).

`_export_instance_worker` wrote through `save_as` straight onto the
output path, and `save_as` creates the file before it streams elements
into it in ascending tag order. A write that raised part-way left its
partial file where it was -- and `dcmread` accepts a truncated dataset,
so a released cohort held an instance that parses and is silently short
of whatever had not been streamed yet, most often Pixel Data
((7FE0,0010) is written last and is the largest element).

The fix writes to a temporary name in the destination directory and
renames onto the real name only when the write finished, so a file under
its real name is a file that was written to the end. Both public write
paths -- `session.export()` and `DicomExporter.write_tree()` -- funnel
through the same worker, so both are exercised here.

The failure recipes are pure data, because `session.export()`'s workers
are always separate processes (`_run_export_batch` passes
`maxtasksperchild=25`), so a monkeypatch in the parent cannot reach the
write.
"""
import os
import sqlite3
from datetime import date

import numpy as np

from isocenter.entities import Instance, Patient, Series, Study
from isocenter.io_handlers import DicomExporter
from isocenter.session import DicomSession

CT_STORAGE = "1.2.840.10008.5.1.4.1.1.2"

CT_REQUIRED = (
    ("0008,0020", "20230101"), ("0008,0030", "120000"),
    ("0008,0060", "CT"),
    ("0018,0050", "1.0"), ("0018,0060", "120"),
    ("0020,0032", ["0", "0", "0"]),
    ("0020,0037", ["1", "0", "0", "0", "1", "0"]),
    ("0028,0030", ["0.5", "0.5"]),
)

#: Overlay Rows, VR `US`. A string here encodes fine into the object
#: graph and raises inside `save_as`, at group `6000` -- after most of
#: the dataset is already on disk, which is what used to leave a
#: readable partial under the real name (#198, #199).
LATE_FAILURE_TAG = "6000,0010"


def _patient(break_instances=()):
    patient = Patient("PAT1", "Original Name")
    study = Study("ST_1", date(2023, 1, 1))
    study.study_time = "120000"
    series = Series("SE_1", "CT", 1)

    for n in range(3):
        inst = Instance(f"1.2.826.0.1.{n}", CT_STORAGE, n + 1)
        inst.file_path = None
        for tag, value in CT_REQUIRED:
            inst.set_attr(tag, value)
        if n in break_instances:
            inst.set_attr(LATE_FAILURE_TAG, "not-a-number")
        inst.set_pixel_data(np.zeros((8, 8), dtype=np.uint16))
        series.instances.append(inst)

    study.series.append(series)
    patient.studies.append(study)
    return patient


def _session(tmp_path, break_instances=()):
    session = DicomSession(str(tmp_path / "atomic.db"))
    session.store.patients.append(_patient(break_instances))
    session.save()
    return session


def _tree(out):
    """Every file under `out`, however named -- partials and temps too."""
    return sorted(p.name for p in out.rglob("*") if p.is_file())


def test_a_failed_write_leaves_nothing_under_the_real_name(tmp_path):
    """The output tree must hold exactly the files the report counts.

    Before the fix this run left three files: two complete, one partial
    under its real name, all three accepted by `dcmread` -- beside a
    report reading "2 of 3". The folder is what a recipient copies, so
    the folder is what has to be true.
    """
    session = _session(tmp_path, break_instances=(1,))
    out = tmp_path / "out"
    try:
        session.export(str(out), show_progress=False)
        db_path = session.store_backend.db_path
    finally:
        session.close()

    assert _tree(out) == ["1.2.826.0.1.0.dcm", "1.2.826.0.1.2.dcm"], _tree(out)

    with sqlite3.connect(db_path) as conn:
        errors = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action_type='ERROR'"
        ).fetchone()[0]
    assert errors == 1, "the arm did not fail the write it was built to fail"


def test_a_failed_write_leaves_no_orphaned_temp_file(tmp_path):
    """The temp name must not become the new way to leak a partial.

    The worker owns the cleanup: it is the only frame that knows a write
    was started and did not finish, and it is always a subprocess, so
    nothing in the parent could do it for it.
    """
    session = _session(tmp_path, break_instances=(0, 1, 2))
    out = tmp_path / "out"
    try:
        session.export(str(out), show_progress=False)
    finally:
        session.close()

    assert _tree(out) == [], _tree(out)


def test_a_clean_export_still_lands_every_file_under_its_real_name(tmp_path):
    """The rename must actually happen, and leave no temp residue."""
    import pydicom

    session = _session(tmp_path)
    out = tmp_path / "out"
    try:
        session.export(str(out), show_progress=False)
    finally:
        session.close()

    names = _tree(out)
    assert names == ["1.2.826.0.1.0.dcm", "1.2.826.0.1.1.dcm",
                     "1.2.826.0.1.2.dcm"], names
    for path in out.rglob("*.dcm"):
        ds = pydicom.dcmread(path)
        assert ds.Rows == 8, "a written file must read back complete"


def test_write_tree_leaves_no_partial_when_it_raises(tmp_path):
    """The serializer path fails by raising, and must fail as cleanly.

    `write_tree` funnels through the same worker, so the same partial
    used to appear in fixture-generator output -- with no session, no
    audit row and no report to contradict it, which made it the quieter
    copy of the same corrupt deliverable.
    """
    import pytest

    out = tmp_path / "tree"
    with pytest.raises(RuntimeError):
        DicomExporter.write_tree(_patient(break_instances=(1,)), str(out),
                                 show_progress=False)

    names = sorted(p.name for p in out.rglob("*") if p.is_file())
    assert names == ["1.2.826.0.1.0.dcm", "1.2.826.0.1.2.dcm"], names

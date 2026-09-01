"""Export-time data loss has to reach the audit log, not just a log line (#126).

`_merge` drops an element it cannot encode and warns. `_export_instance_worker`
writes a waveform header with no samples and warns. Both are real losses in
the exported artefact, and both were reported only to the logger -- because
the code that notices them runs inside a worker that may be in a subprocess
with no store handle.

A warning is not a compliance trail. #36 set the precedent at ingest: warn
*and* write a `DATA_LOSS` audit entry. This is the same channel for the
export side, which means the worker has to carry the losses back rather than
report them where it finds them.
"""
import logging
import os
import sqlite3

import numpy as np
import pydicom
import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from isocenter import io_handlers
from isocenter.entities import Instance, Patient, Series, Study
from isocenter.io_handlers import (DicomExporter, ExportOutcome,
                                   LOSS_SCOPE_PRIVATE, LOSS_SCOPE_STANDARD)
from isocenter.session import DicomSession, _ExportOptions

# `_fallback_encoding` returns None for this: not bytes, not a number, not
# a string. It is the one shape that reaches the "no VR fits" arm.
UNENCODABLE = object()

PRIVATE_TAG = "0009,1003"


def _write_src(folder):
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.7"
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian

    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "PAT1", "DOE^JOHN"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = meta.MediaStorageSOPClassUID
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "OT", 1, 1
    ds.StudyDate = "20230101"
    ds.Rows = ds.Columns = 4
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit = 7
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelRepresentation = 0
    ds.PixelData = np.zeros((4, 4), dtype=np.uint8).tobytes()

    ds.save_as(os.path.join(folder, "one.dcm"), enforce_file_format=True)
    return ds.SOPInstanceUID


def _instances(session):
    return [i for p in session.store.patients for st in p.studies
            for se in st.series for i in se.instances]


def _data_loss_rows(db_path):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT entity_uid, details, loss_scope FROM audit_log "
            "WHERE action_type='DATA_LOSS'").fetchall()


def _export(tmp_path, plant=None):
    """Ingest one instance, optionally break it, export, return audit rows."""
    src = tmp_path / "src"
    src.mkdir()
    uid = _write_src(str(src))

    session = DicomSession(persistence_file=str(tmp_path / "loss.db"))
    try:
        session.ingest(str(src))
        if plant is not None:
            for inst in _instances(session):
                plant(inst)
        session.export(str(tmp_path / "out"), format="dicom",
                       show_progress=False)
        db_path = session.store_backend.db_path
    finally:
        session.close()

    return uid, _data_loss_rows(db_path)


def test_an_unencodable_tag_writes_a_data_loss_audit_entry(tmp_path):
    """The #118 warning arm, now with the compliance trail #36 established."""
    uid, rows = _export(
        tmp_path,
        plant=lambda inst: inst.attributes.__setitem__(PRIVATE_TAG, UNENCODABLE))

    assert len(rows) == 1, rows
    entity_uid, details, _scope = rows[0]
    assert entity_uid == uid
    assert PRIVATE_TAG in details


def test_an_unencodable_private_tag_is_recorded_as_private_scope(tmp_path):
    """An element the caller asked to keep and did not get is graded.

    This is the export-side twin of the ingest loss: the vendor block
    reached the graph, survived `remove_private_tags=False`, and then
    did not reach the file. The worker classifies it, because
    `_report_export_losses` in the parent has only the message by then
    (#146).
    """
    _uid, rows = _export(
        tmp_path,
        plant=lambda inst: inst.attributes.__setitem__(PRIVATE_TAG, UNENCODABLE))

    assert [scope for _u, _d, scope in rows] == [LOSS_SCOPE_PRIVATE], rows


def test_a_backslash_bearing_atom_is_a_loud_loss_not_a_wrong_element(tmp_path):
    """#190/#195, the VM > 1 half, end to end.

    `\\` is DICOM's value separator (PS3.5 6.2), so no text VR can carry
    both a backslash-bearing atom and the element's arity: `LO` splits
    it (VM 2 -> 3, silently, which is what shipped), and a `UT` join is
    ambiguous in the same way. This shape is reachable only through
    `set_attr` -- pydicom splits source elements at ingest, so
    file-sourced atoms are separator-free -- and the answer is the one
    #165's CHANGELOG already gives for a partial element: a loud loss
    beats a silent wrong value. So: a `DATA_LOSS` row scoped `PRIVATE`,
    a `REVIEW_REQUIRED` grade, and no element in the file.
    """
    src = tmp_path / "src"
    src.mkdir()
    uid = _write_src(str(src))
    out = tmp_path / "out"

    session = DicomSession(persistence_file=str(tmp_path / "bs.db"))
    try:
        session.ingest(str(src))
        for inst in _instances(session):
            inst.set_attr("0009,1022", ["se\\rial", "ok"])
        session.export(str(out), format="dicom", show_progress=False)
        report_path = tmp_path / "report.md"
        session.generate_report(str(report_path))
        db_path = session.store_backend.db_path
    finally:
        session.close()

    written = [os.path.join(r, f) for r, _, fs in os.walk(str(out))
               for f in fs if f.endswith(".dcm")]
    assert len(written) == 1, "the loss is one element, not the file"
    ds = pydicom.dcmread(written[0])
    assert 0x00091022 not in ds, (
        "a wrong element was written instead of the loss being reported",
        ds[0x00091022])

    rows = _data_loss_rows(db_path)
    assert len(rows) == 1, rows
    entity_uid, details, scope = rows[0]
    assert entity_uid == uid
    assert "0009,1022" in details, details
    assert scope == LOSS_SCOPE_PRIVATE, rows

    assert "REVIEW_REQUIRED" in report_path.read_text()


def test_a_clean_export_records_no_data_loss(tmp_path):
    """Nothing lost, nothing logged -- or the entry means nothing."""
    _uid, rows = _export(tmp_path)
    assert rows == []


def test_write_tree_still_warns_when_there_is_no_backend_to_log_to(tmp_path,
                                                                   caplog):
    """The serializer path has no session, and must not go quiet.

    Moving the report from the worker to the parent is only safe if the
    parent reports it on *both* public paths. `write_tree` can never
    supply a store handle, so if the warning rode the audit entry it
    would vanish for every fixture generator in `scripts/`.
    """
    src = tmp_path / "src"
    src.mkdir()
    _write_src(str(src))

    session = DicomSession(persistence_file=str(tmp_path / "wt.db"))
    try:
        session.ingest(str(src))
        for inst in _instances(session):
            inst.attributes[PRIVATE_TAG] = UNENCODABLE
        patient = session.store.patients[0]
        with caplog.at_level(logging.WARNING):
            DicomExporter.write_tree(patient, str(tmp_path / "wt"),
                                     show_progress=False)
    finally:
        session.close()

    msgs = [r.getMessage() for r in caplog.records
            if r.levelno >= logging.WARNING]
    assert any(PRIVATE_TAG in m and "not exported" in m for m in msgs), msgs


def test_the_export_still_succeeds_despite_the_loss(tmp_path):
    """A dropped element is data loss, not a failed write.

    The file is still produced -- with the other elements intact -- which
    is exactly why the loss needs recording somewhere durable.
    """
    src = tmp_path / "src"
    src.mkdir()
    _write_src(str(src))
    out = tmp_path / "out"

    session = DicomSession(persistence_file=str(tmp_path / "ok.db"))
    try:
        session.ingest(str(src))
        for inst in _instances(session):
            inst.attributes[PRIVATE_TAG] = UNENCODABLE
        session.export(str(out), format="dicom", show_progress=False)
    finally:
        session.close()

    written = [os.path.join(r, f) for r, _, fs in os.walk(str(out))
               for f in fs if f.endswith(".dcm")]
    assert len(written) == 1
    ds = pydicom.dcmread(written[0])
    assert ds.PatientID == "PAT1"


def test_one_tag_lost_at_two_levels_is_reported_once():
    """The surviving reason `_merge` dedupes its losses (#179).

    The dedupe used to be load-bearing for a blunt reason: the worker
    merged `inst.attributes` twice, so every loss arrived in duplicate.
    That copy-paste is gone, and the dedupe is not -- because the four
    remaining merges overlap by tag. `(0010,0010)` is in the instance
    mapping *and* in the patient mapping stamped over it, so one value
    the encoder cannot take is offered to `_merge` twice, fails
    identically twice, and would be counted twice in section 3 of the
    compliance report.

    Reachable in principle rather than routine -- the overlapping tags
    all have dictionary VRs, so it takes a malformed value rather than
    an exotic one. That is a thinner reason than the one it replaces,
    which is why it is written down here instead of left to be inferred.
    """
    losses = []
    ds = pydicom.Dataset()

    # The two mappings are distinct objects on purpose: this is the
    # instance/patient overlap, not the same dict merged twice.
    DicomExporter._merge(ds, {"0010,0010": UNENCODABLE}, losses)
    DicomExporter._merge(ds, {"0010,0010": UNENCODABLE}, losses)

    assert len(losses) == 1, losses
    assert losses[0][0] == LOSS_SCOPE_STANDARD, losses


def test_a_waveform_with_no_samples_writes_a_data_loss_audit_entry(tmp_path):
    """The second loss on the channel, end to end (#126).

    Its unit-level cousin in `test_waveform_dicom_roundtrip.py` proves the
    worker *reports* it. This proves the report is *delivered*: through a
    real session, whose workers are in subprocesses, into the audit table.
    """
    from scripts.generate_waveform_test_data import build_ecg_dataset

    src = tmp_path / "src"
    src.mkdir()
    ds = build_ecg_dataset(num_samples=100)
    del ds.WaveformSequence[0].WaveformData
    pydicom.dcmwrite(str(src / "ecg.dcm"), ds, enforce_file_format=True)

    session = DicomSession(persistence_file=str(tmp_path / "wave.db"))
    try:
        session.ingest(str(src))
        session.export(str(tmp_path / "out"), format="dicom",
                       show_progress=False)
        db_path = session.store_backend.db_path
    finally:
        session.close()

    rows = _data_loss_rows(db_path)
    details = [d for _uid, d, _s in rows]
    assert any("does not contain" in d for d in details), details
    # Waveform Data is (5400,1010) -- an even group, so under the #146
    # rule this loss is reported and not graded.
    assert [s for _u, _d, s in rows] == [LOSS_SCOPE_STANDARD], rows


def test_a_lost_element_does_not_make_the_export_report_zero_successes(
        tmp_path, monkeypatch):
    """The success count survives the second pass over the results.

    Reporting the losses walks `results`; counting successes walks it
    again. `run_parallel` returns a generator when asked to
    (`return_generator=True`), and if an export ever asked, the first
    walk would consume it and every export would report `0/N` written --
    while writing all N files, so nothing else in the suite would notice.
    `_run_export_batch` responds to a short count by printing a
    partial-failure warning and *not* raising.

    No export site asks today, so the generator is forced here rather
    than waited for. A test that only ran the list case would pass
    whether or not the results were materialized, and pin nothing.
    """
    real = io_handlers.run_parallel
    monkeypatch.setattr(io_handlers, "run_parallel",
                        lambda *a, **kw: iter(real(*a, **kw)))

    src = tmp_path / "src"
    src.mkdir()
    _write_src(str(src))

    session = DicomSession(persistence_file=str(tmp_path / "count.db"))
    try:
        session.ingest(str(src))
        tasks, _ = session._build_export_plan(
            _ExportOptions(str(tmp_path / "out"), None, None, False),
            {p.patient_id for p in session.store.patients})
        for task in tasks:
            task.instance.attributes[PRIVATE_TAG] = UNENCODABLE
        summary = DicomExporter.export_batch(
            tasks, show_progress=False, total=len(tasks),
            store_backend=session.store_backend)
    finally:
        session.close()

    assert summary.written == len(tasks), "a loss is not a failed write"
    assert summary.failures == [], "a loss is not a failed write"


# --------------------------------------------------------------------------
# A loss on an instance whose write then failed (#240)
# --------------------------------------------------------------------------

CT_STORAGE = "1.2.840.10008.5.1.4.1.1.2"

#: The correction a loss row on a failed outcome must carry. Asserted as
#: a substring so the tests pin the statement, not the sentence around it.
NOT_WRITTEN = "file itself was not written"


class _RecordingStore:
    """The audit-writing half of a store backend, and nothing else."""

    def __init__(self):
        self.rows = []

    def log_audit(self, action_type, entity_uid, details, loss_scope=None,
                  element_tag=None):
        self.rows.append((action_type, entity_uid, details, loss_scope))


def test_a_loss_on_a_failed_outcome_says_the_file_was_not_written():
    """The parent-side contract of #240, pinned where the row is written.

    A worker can append a loss and *then* fail -- the element was
    genuinely dropped from the in-memory copy, and the file was never
    written. `_report_export_losses` filed the row without asking
    `r.ok`, so section 3 of the compliance report said an element "was
    not exported" from a file that does not exist, beside the `ERROR`
    row for the same instance.

    The row is kept, not suppressed: the observation is true, and a
    compliance record that drops an observation is its own small lie
    (#181). What changes is the statement -- a loss belonging to a
    failed outcome must say the file itself was not written, and a loss
    on a successful outcome must read exactly as the worker wrote it.
    """
    store = _RecordingStore()
    results = [
        ExportOutcome(ok=True, output_path="/out/a.dcm",
                      sop_instance_uid="A",
                      losses=[(LOSS_SCOPE_STANDARD, "loss on a success.")]),
        ExportOutcome(ok=False, output_path="/out/b.dcm",
                      sop_instance_uid="B",
                      losses=[(LOSS_SCOPE_PRIVATE, "loss on a failure.")],
                      error=ValueError("Validation Errors: ['...']")),
    ]

    count = DicomExporter._report_export_losses(results, store)

    assert count == 2, "the failed outcome's loss must still be reported"
    by_uid = {uid: (details, scope)
              for _action, uid, details, scope in store.rows}
    assert by_uid["A"] == ("loss on a success.", LOSS_SCOPE_STANDARD), (
        "a loss on a written file must read exactly as the worker wrote it")
    details, scope = by_uid["B"]
    assert details.startswith("loss on a failure."), details
    assert NOT_WRITTEN in details, details
    assert scope == LOSS_SCOPE_PRIVATE, "the annotation must not touch the scope"


def test_a_float16_loss_on_a_failed_write_names_the_missing_file(tmp_path):
    """#240's shape, end to end through `session.export()`.

    A graph-built SR-modality instance with a float16 array: the
    float16 arm appends its loss row (SR is not an image modality, so
    it does not raise), and `_finalize_dataset`'s IOD validation then
    fails the write. The SOP class is CT Image Storage rather than the
    issue's Secondary Capture because `IODValidator` only carries rules
    for CT -- an SC dataset validates trivially here, and the modality
    attribute, not the SOP class, is what routes the float16 arm. Zero
    files reach disk. The `DATA_LOSS` row must not read as a statement
    about a written file, and the `ERROR` row must still be there --
    the two rows are two true statements about one instance, not a
    contradiction.
    """
    session = DicomSession(str(tmp_path / "failloss.db"))
    uid = "1.2.826.0.1.240"
    out = tmp_path / "out"
    out.mkdir()

    patient = Patient("PAT1", "Test^Patient")
    study = Study("ST_1", "20230101")
    series = Series("SE_1", "SR", 1)
    inst = Instance(uid, CT_STORAGE, 1)
    inst.file_path = None
    inst.set_attr("0008,0060", "SR")
    inst.set_pixel_data(np.zeros((8, 8), dtype=np.float16))
    series.instances.append(inst)
    study.series.append(series)
    patient.studies.append(study)
    session.store.patients.append(patient)

    try:
        session.save()
        session.export(str(out), format="dicom", show_progress=False)
        db_path = session.store_backend.db_path
    finally:
        session.close()

    files = sorted(p.name for p in out.rglob("*.dcm"))
    assert files == [], "the premise: this write must fail"

    with sqlite3.connect(db_path) as conn:
        error_uids = [u for (u,) in conn.execute(
            "SELECT entity_uid FROM audit_log WHERE action_type='ERROR'")]
    assert error_uids == [uid], "the failure row is #181's and must stay"

    rows = _data_loss_rows(db_path)
    assert len(rows) == 1, rows
    entity_uid, details, scope = rows[0]
    assert entity_uid == uid
    assert "float16" in details, details
    assert scope == LOSS_SCOPE_STANDARD, rows
    assert NOT_WRITTEN in details, (
        "the row claims an element was dropped from a file that was "
        "never written", details)

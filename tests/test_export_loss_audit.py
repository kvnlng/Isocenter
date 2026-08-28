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
from isocenter.io_handlers import (DicomExporter, LOSS_SCOPE_PRIVATE,
                                   LOSS_SCOPE_STANDARD)
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

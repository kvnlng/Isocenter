"""A per-file ingest failure is a failure the caller can see (#211).

Ingesting a file pydicom refuses -- a 16-bit `YBR_FULL` whose colour
conversion raises, or a file that is not DICOM at all -- used to
produce one console line (`ERROR: Import Failed`), zero patients, no
exception, no audit row, and a `None` return. A caller ingesting a
directory and then querying the store saw an empty (or short) result
with no programmatic way to learn that anything went wrong, which file,
or why. A run that ingested 460 of 500 files reported exactly like a
clean run.

This mirrors #181, which closed the same hole on the export side, and
the scoping follows its vocabulary deliberately: a rejected file at
ingest is a *failure* (`ERROR`), not a *loss* (`DATA_LOSS`) -- nothing
was indexed, so nothing the store holds is smaller than it claims to
be. #146's loss scopes describe elements missing from data that was
ingested; a file that never entered the store has no row there to
scope.

Three surfaces, each pinned below:

- an `ERROR` audit row per rejected file, carrying the path and the
  reason -- the same row shape `_report_export_failures` writes;
- an `IngestSummary` returned by `session.ingest()` (and by
  `DicomImporter.import_files`), so "ingested 460 of 500" is
  discoverable without reading console output;
- the compliance report: `ERROR` rows land in `get_audit_errors()`,
  which feeds the Exceptions section and bars the `PASS` grade -- so a
  cohort that silently lost files does not grade as though it did not.

The route is general: *any* per-file failure takes it -- the worker's
blanket `except`, its decompression arm, and a parent-side linkage
failure all return through the same channel -- so the tests drive two
unrelated triggers and assert the same treatment.
"""
import os
import pathlib
import sqlite3

import numpy as np
import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from isocenter.io_handlers import IngestSummary
from isocenter.session import DicomSession

SC = "1.2.840.10008.5.1.4.1.1.7"


def _base_ds():
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SC
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian

    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "P1", "DOE^JANE"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = SC
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "OT", 1, 1
    ds.StudyDate = "20240115"
    return ds


def _write_good(folder, name="good.dcm"):
    ds = _base_ds()
    ds.Rows = ds.Columns = 4
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelData = np.zeros((4, 4), dtype=np.uint8).tobytes()
    path = os.path.join(folder, name)
    ds.save_as(path, enforce_file_format=True)
    return path


def _write_ybr16(folder, name="ybr16.dcm"):
    """The trigger as filed: pydicom refuses the colour conversion.

    16-bit `YBR_FULL` raises `Invalid ndarray.dtype 'uint16' for color
    space conversion` inside `ds.pixel_array`, which `ingest_worker`'s
    decompression arm turns into an error return -- arguably correct
    behaviour on pydicom's part; what is under test is what Isocenter
    does with the refusal.
    """
    ds = _base_ds()
    ds.Rows = ds.Columns = 4
    ds.BitsAllocated = ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 3
    ds.PlanarConfiguration = 0
    ds.PhotometricInterpretation = "YBR_FULL"
    ds.PixelData = np.zeros((4, 4, 3), dtype=np.uint16).tobytes()
    path = os.path.join(folder, name)
    ds.save_as(path, enforce_file_format=True)
    return path


def _write_not_dicom(folder, name="junk.dcm"):
    """`force=True` parses it to a dataset with no SOPInstanceUID."""
    path = os.path.join(folder, name)
    with open(path, "wb") as fh:
        fh.write(b"this is not a DICOM file, whatever the extension says")
    return path


@pytest.fixture(params=["not_dicom", "ybr16"])
def partial_ingest(request, tmp_path):
    """One good file and one rejected one, through the public path."""
    src = tmp_path / "src"
    src.mkdir()
    _write_good(str(src))
    writer = _write_not_dicom if request.param == "not_dicom" else _write_ybr16
    bad_path = writer(str(src))

    report = tmp_path / "report.md"
    session = DicomSession(persistence_file=str(tmp_path / "ingest.db"))
    try:
        summary = session.ingest(str(src))
        # Anonymize so `audit_summary` is non-empty and the baseline
        # grade would be PASS; without it an empty summary grades
        # REVIEW_REQUIRED on its own and the grade assertion would pass
        # with the fix deleted.
        session.anonymize()
        session.generate_report(str(report))
        db_path = session.store_backend.db_path
        n_instances = sum(
            len(se.instances)
            for p in session.store.patients
            for st in p.studies for se in st.series)
    finally:
        session.close()

    return {
        "summary": summary,
        "bad_path": bad_path,
        "db_path": db_path,
        "n_instances": n_instances,
        "report": report.read_text(encoding="utf-8"),
    }


def _error_rows(db_path):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT entity_uid, details FROM audit_log "
            "WHERE action_type='ERROR'").fetchall()


def test_the_summary_reaches_the_caller_with_the_count_and_the_reason(
        partial_ingest):
    """`ingest()` answers "460 of 500" programmatically, not on stdout."""
    summary = partial_ingest["summary"]
    assert isinstance(summary, IngestSummary)
    assert summary.ingested == 1
    assert summary.failed == 1
    (path, reason), = summary.failures
    assert path == partial_ingest["bad_path"]
    assert reason, "a failure with no reason is a console line with extra steps"
    # The store agrees with the summary's claim.
    assert partial_ingest["n_instances"] == summary.ingested


def test_a_rejected_file_writes_an_error_audit_row(partial_ingest):
    """Path and reason, in the compliance trail rather than a log file.

    `ERROR` and not `DATA_LOSS`: the scoping decision. Loss rows
    describe elements missing from ingested data; this file was never
    indexed at all, which is the failure vocabulary #181 gave the
    export side. The entity column carries the path because a file that
    failed to parse has no SOP Instance UID to be named by -- the same
    fallback `_report_export_failures` uses.
    """
    rows = _error_rows(partial_ingest["db_path"])
    assert len(rows) == 1, rows
    uid, details = rows[0]
    assert uid == partial_ingest["bad_path"]
    assert "|" not in details and "\n" not in details, (
        "the detail is rendered into a markdown table row")


def test_the_report_does_not_grade_pass_over_a_partial_ingest(partial_ingest):
    """The grade is the headline; a silent 8% loss must move it."""
    content = partial_ingest["report"]
    assert "| **Validation Status** | **REVIEW_REQUIRED** |" in content, content
    assert "*No exceptions or errors were recorded.*" not in content, content


def test_a_clean_ingest_returns_a_clean_summary(tmp_path):
    """The control, and the re-ingest arm: skipped is not failed.

    The third ingest is the one that costs a line and is worth it.
    `IngestSummary.skipped` is set in TWO places -- the early return
    taken when *every* file is already known, and the terminal return
    of the general path when only some are -- and until #284 only the
    first was asserted anywhere. That mattered because #284's ruling
    treats the "Skipping N already imported files" log line as
    best-effort *on the grounds that* `skipped` carries the same number;
    a justification pinned on only one of the two branches, while the
    log line fires on both, is half a justification. Ingesting a folder
    that gained a file exercises the other branch.

    Both branches were cited by line number until #329, and both
    numbers had drifted hundreds of lines from the code they meant.
    Neither rule in `tests/test_source_citations.py` could see it: one
    was still in range and so graded as fine, the other named no file
    at all and so matched nothing. They are named rather than numbered
    now, because the number was decorative -- the prose identifies each
    branch on its own -- and a decorative number is one nobody notices
    going wrong. That is the opposite of the five
    `entity.mark_modified()` citations, where the line number *is* the
    identity of one of five near-identical calls, which is why those
    are written in the quoting grammar and pinned and these are not.
    """
    src = tmp_path / "src"
    src.mkdir()
    _write_good(str(src))

    session = DicomSession(persistence_file=str(tmp_path / "clean.db"))
    try:
        first = session.ingest(str(src))
        again = session.ingest(str(src))
        _write_good(str(src), name="second.dcm")
        mixed = session.ingest(str(src))
        db_path = session.store_backend.db_path
    finally:
        session.close()

    assert first.ingested == 1
    assert first.failed == 0
    assert first.failures == []

    # A file already in the store is skipped, not re-ingested and not
    # failed -- three different answers, kept apart.
    assert again.ingested == 0
    assert again.failed == 0
    assert again.skipped == 1

    # The general path, where there is real work to do as well: the
    # known file is still counted as skipped rather than disappearing
    # because the ingest did not return early.
    assert mixed.ingested == 1, mixed
    assert mixed.failed == 0
    assert mixed.skipped == 1, (
        "a folder that gained one file reported the already-known file "
        "as neither ingested nor skipped")

    assert _error_rows(db_path) == []

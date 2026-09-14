"""An export failure's text names what went wrong, never the path (#588, P8).

Export paths are built from the graph: a DICOM file lands under
`Subject_<Patient ID>/...` and a WFDB record is named `<Patient ID>_...`.
An `OSError`'s `str()` embeds the filename it failed on, so two channels
carried that path wherever an exception was spelled into them:

* the WFDB exporter's per-record `ERROR` row, which interpolated `{e}` --
  persisted in the store, rendered into the compliance report, and carried
  by `ExportError.failures` (#588);
* the DICOM export worker's console line, which printed
  `ctx.output_path` and then the exception to stderr (P8, bunch E).

Both now go through `describe_exception_without_paths`: the exception type
leads, as everywhere else (#435), and an `OSError` -- the exception that
carries a filename by construction -- is spelled by its `strerror` alone.
The failures below are real `OSError`s, raised by `os.makedirs` against an
output folder that is a regular file, not hand-built ones.
"""
import os

import numpy as np
import pytest

from isocenter.entities import Instance
from isocenter.exporters.wfdb import WfdbExporter
from isocenter.io_handlers import (ExportContext, ExportError,
                                   _export_instance_worker)
from tests.test_wfdb_partial_export_is_audited import (
    _instances, _rows, _session_with_two_ecgs)

CT_STORAGE = "1.2.840.10008.5.1.4.1.1.2"
CT_REQUIRED = (
    ("0008,0020", "20230101"), ("0008,0030", "120000"),
    ("0008,0060", "CT"),
    ("0018,0050", "1.0"), ("0018,0060", "120"),
    ("0020,0032", ["0", "0", "0"]),
    ("0020,0037", ["1", "0", "0", "0", "1", "0"]),
    ("0028,0030", ["0.5", "0.5"]),
)


def test_a_wfdb_record_that_cannot_be_written_records_no_path(tmp_path):
    session = _session_with_two_ecgs(tmp_path, name="wfdb_nopath.db")
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the output folder should be")
    try:
        ids = [p.patient_id for p in session.store.patients]
        uids = sorted(i.sop_instance_uid for i in _instances(session))
        with pytest.raises(ExportError) as raised:
            session.export(str(blocker), format="wfdb")
        rows = _rows(session, "ERROR")
    finally:
        session.close()

    details = [detail for _uid, detail in rows]
    assert len(details) == 2, rows
    for detail in details + [d for _u, d in raised.value.failures]:
        # The failure is real: makedirs under a regular file.
        assert "NotADirectoryError" in detail, detail
        assert str(blocker) not in detail, detail
        assert "Subject_" not in detail, detail
        for patient_id in ids:
            assert patient_id not in detail, detail
    assert sorted(u for u, _d in raised.value.failures) == uids


def test_a_wfdb_failure_caused_by_an_os_error_records_no_path(tmp_path,
                                                             monkeypatch):
    """The cause is spelled the same way: `raise ... from` an OSError put
    the filename back through `describe_exception`'s `(caused by ...)`."""
    session = _session_with_two_ecgs(tmp_path, name="wfdb_cause.db")

    def failing(self, *args, **kwargs):
        try:
            open(os.path.join(str(tmp_path), "Subject_WF_A", "WF_A_1_1.hea"),
                 encoding="utf-8")
        except OSError as exc:
            raise RuntimeError("could not write the header") from exc

    monkeypatch.setattr(WfdbExporter, "_write_instance", failing)
    try:
        with pytest.raises(ExportError):
            session.export(str(tmp_path / "out"), format="wfdb")
        rows = _rows(session, "ERROR")
    finally:
        session.close()

    assert rows, "no ERROR row was recorded"
    for _uid, detail in rows:
        assert detail.endswith(
            "RuntimeError: could not write the header (caused by "
            "FileNotFoundError: No such file or directory)"), detail
        assert "WF_A" not in detail, detail


def _ct_instance(uid):
    inst = Instance(uid, CT_STORAGE, 1)
    inst.file_path = None
    for tag, value in CT_REQUIRED:
        inst.set_attr(tag, value)
    inst.set_pixel_data(np.zeros((8, 8), dtype=np.uint16))
    return inst


def test_a_dicom_write_failure_prints_no_path_to_the_console(tmp_path, capfd):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the output folder should be")
    uid = "1.2.826.0.1.588"
    output_path = os.path.join(str(blocker), "Subject_P123", "Study_1",
                               "Series_1", f"{uid}.dcm")
    capfd.readouterr()

    outcome = _export_instance_worker(ExportContext(
        instance=_ct_instance(uid), output_path=output_path,
        patient_attributes={"0010,0010": "ANON", "0010,0020": "P123"},
        study_attributes={"0020,000d": "1.2.826.0.2.588"},
        series_attributes={"0020,000e": "1.2.826.0.3.588"}))
    err = capfd.readouterr().err

    assert outcome.ok is False and isinstance(outcome.error, OSError), outcome
    assert "ERROR: Export failed" in err, err
    assert uid in err, err
    assert "NotADirectoryError: Not a directory" in err, err
    assert "Subject_P123" not in err, err
    assert str(blocker) not in err, err


def test_an_os_error_is_spelled_by_its_type_and_strerror():
    from isocenter.logger import describe_exception_without_paths

    exc = PermissionError(13, "Permission denied", "/out/Subject_P1/r.dat",
                          None, "/out/Subject_P1/r.hea")
    assert describe_exception_without_paths(exc) == (
        "PermissionError: Permission denied")


def test_an_os_error_with_no_strerror_is_spelled_by_its_type():
    """`OSError("...")` keeps its whole message in `args`, and a message
    built around a path is exactly what this exists to leave out."""
    from isocenter.logger import describe_exception_without_paths

    exc = OSError("cannot open /out/Subject_P1/r.dat")
    assert describe_exception_without_paths(exc) == "OSError"


def test_any_other_exception_is_spelled_as_describe_exception_spells_it():
    """Only `OSError` carries a filename by construction. Every other
    message is kept, as `describe_exception` keeps it: those are the
    reasons the report exists to show."""
    from isocenter.logger import describe_exception_without_paths

    try:
        try:
            raise KeyError("x")
        except KeyError as inner:
            raise RuntimeError("channel table is malformed") from inner
    except RuntimeError as exc:
        assert describe_exception_without_paths(exc) == (
            "RuntimeError: channel table is malformed (caused by KeyError: 'x')")


# --- The DICOM export's `ERROR` row -------------------------------------
#
# The WFDB rows above were fixed first (#588) and the DICOM worker's console
# line with them (P8), which left the DICOM `ERROR` row -- the one
# `_report_export_failures` persists, renders into the report and hands
# `ExportError.failures` -- as `Export failed for <output path>: <exception>`.
# The output path is `<folder>/Subject_<Patient ID>/...`, and a
# `NotADirectoryError` repeats it. It now has the WFDB row's shape: the
# instance, the exception type, a path-free reason.


def test_a_dicom_instance_that_cannot_be_written_records_no_path(tmp_path):
    from tests.test_export_failure_audit import _session

    session = _session(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the output folder should be")
    report = tmp_path / "report.md"
    try:
        session.anonymize()
        ids = [p.patient_id for p in session.store.patients]
        uids = sorted(i.sop_instance_uid for p in session.store.patients
                      for st in p.studies for se in st.series
                      for i in se.instances)
        with pytest.raises(ExportError) as raised:
            session.export(str(blocker), format="dicom", show_progress=False)
        rows = _rows(session, "ERROR")
        session.generate_report(str(report))
    finally:
        session.close()

    assert sorted(u for u, _d in rows) == uids, rows
    assert sorted(u for u, _d in raised.value.failures) == uids
    for uid, detail in rows + raised.value.failures:
        # The failure is real: makedirs under a regular file.
        assert detail == (f"Export failed for instance {uid}: "
                          "NotADirectoryError: Not a directory"), detail
    text = report.read_text(encoding="utf-8")
    for detail in [d for _u, d in rows]:
        assert detail in text, "the ERROR row did not reach the report"
    for patient_id in ids + ["PAT1"]:
        assert f"Subject_{patient_id}" not in text, patient_id


def test_a_dicom_failure_row_spells_an_os_error_without_its_filename():
    from isocenter.io_handlers import DicomExporter, ExportOutcome

    error = PermissionError(13, "Permission denied",
                            "/out/Subject_P1/Study_1/Series_1/1.2.dcm")
    failures = DicomExporter._report_export_failures([ExportOutcome(
        ok=False, output_path="/out/Subject_P1/Study_1/Series_1/1.2.dcm",
        sop_instance_uid="1.2", error=error)])

    assert failures == [
        ("1.2", "Export failed for instance 1.2: PermissionError: Permission denied")]


def test_a_dicom_failure_with_no_uid_is_keyed_without_its_path():
    """The row used to fall back to the output path for its entity UID
    too. The export plan names every file after a UID, so this is not
    reachable from `session.export()`; the fallback is still a path."""
    from isocenter.io_handlers import DicomExporter, ExportOutcome

    failures = DicomExporter._report_export_failures([ExportOutcome(
        ok=False, output_path="/out/Subject_P1/Study_1/Series_1/x.dcm",
        sop_instance_uid=None, error=KeyError())])

    assert failures == [(
        "UNKNOWN",
        "Export failed for an instance with no SOP Instance UID: KeyError")]


def test_a_dead_export_worker_row_spells_an_os_error_without_its_filename():
    from isocenter.io_handlers import DicomExporter

    failures = DicomExporter._report_export_failures(
        [FileNotFoundError(2, "No such file or directory", "/out/Subject_P1")])

    assert failures == [("UNKNOWN", "Export worker failed: "
                         "FileNotFoundError: No such file or directory")]


def test_a_readback_that_cannot_open_the_written_file_names_no_path(tmp_path):
    """The readback check reads the temp file the worker just wrote,
    `<output path>.<pid>.tmp`, and spelled the read's exception into its
    own message -- so an `OSError` there put `Subject_<Patient ID>/...`
    back into the row above, inside the `RuntimeError`'s text where
    `describe_exception_without_paths` cannot reach it."""
    from isocenter.io_handlers import _verify_readback
    from isocenter.logger import describe_exception_without_paths

    missing = tmp_path / "Subject_P123" / "1.2.dcm.4242.tmp"
    with pytest.raises(RuntimeError) as raised:
        _verify_readback(str(missing), None)

    text = describe_exception_without_paths(raised.value)
    assert "FileNotFoundError" in text, text
    assert "Subject_P123" not in text, text


# --- The read door: the source path -------------------------------------
#
# `get_pixel_data()` re-reads the source file at export, and its two
# failure messages named that file: `Lazy load failed for <source path>`
# and `Failed to decompress pixel data for <file name>`. Both reach the
# export's `ERROR` row, the report and `ExportError` whole -- they are a
# `RuntimeError`'s own words -- and a source tree is often named for the
# patient, so an anonymized session's report still named them (review of
# #589). They name the instance now, as `Pixel Loader failed` already did.
# The pipeline half is `test_float_pixel_data_export.py`'s #226 tests.

PATIENT_NAMED = "Doe_Jane_MRN4455"


def _source_instance(tmp_path):
    from pydicom.dataset import FileDataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian, generate_uid

    folder = tmp_path / PATIENT_NAMED
    folder.mkdir()
    path = folder / f"{PATIENT_NAMED}_1.dcm"
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = CT_STORAGE
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.SOPClassUID, ds.SOPInstanceUID = CT_STORAGE, meta.MediaStorageSOPInstanceUID
    ds.Rows = ds.Columns = 4
    ds.BitsAllocated = ds.BitsStored = 16
    ds.HighBit, ds.PixelRepresentation, ds.SamplesPerPixel = 15, 0, 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelData = np.zeros((4, 4), dtype=np.uint16).tobytes()
    ds.save_as(str(path), enforce_file_format=True)
    return Instance(ds.SOPInstanceUID, CT_STORAGE, 1, file_path=str(path))


@pytest.mark.parametrize("raised, words", [
    # "decompress" in the message is what selects the codecs arm.
    (RuntimeError("Unable to decompress 'JPEG 2000' pixel data"),
     "Failed to decompress pixel data for instance"),
    # ... and its `Underlying Error:` line spells the exception again.
    (OSError(5, "could not decompress frame 0", f"/src/{PATIENT_NAMED}/x.dcm"),
     "Failed to decompress pixel data for instance"),
    (ValueError("The pixel data is truncated"), "Lazy load failed for instance"),
    # An `OSError` from the read repeats the path in its own `str()`.
    (PermissionError(13, "Permission denied", f"/src/{PATIENT_NAMED}/x.dcm"),
     "Lazy load failed for instance"),
])
def test_a_source_read_failure_names_the_instance_not_the_file(
        tmp_path, monkeypatch, raised, words):
    import isocenter.entities as entities

    inst = _source_instance(tmp_path)

    def refuse(_ds):
        raise raised

    monkeypatch.setattr(entities, "_decode_with_pydicom", refuse)
    with pytest.raises(RuntimeError) as caught:
        inst.get_pixel_data()

    message = str(caught.value)
    assert message.startswith(f"{words} {inst.sop_instance_uid}"), message
    assert PATIENT_NAMED not in message, message

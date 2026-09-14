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

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

    monkeypatch.setattr(entities, "_decode_from_file", refuse)
    with pytest.raises(RuntimeError) as caught:
        inst.get_pixel_data()

    message = str(caught.value)
    assert message.startswith(f"{words} {inst.sop_instance_uid}"), message
    assert PATIENT_NAMED not in message, message


# --- pydicom's writer: a traceback inside the message (#1053) -------------
#
# pydicom 3.0.2 writes each element inside `tag_in_exception`, which
# re-raises whatever the element's writer raised as the same class with the
# message `With tag (gggg,eeee) got exception: <message>\n<the whole
# formatted traceback>`. The export's `ERROR` row, `ExportError`'s message,
# the worker's console line and `write_tree()`'s `RuntimeError` all quoted
# that message whole, so each carried a Python traceback, and with it the
# absolute path of the install (`.../site-packages/pydicom/tag.py`), which
# names the home directory. Measured on 067982ec, 3.12 and 3.14t, for
# every refusal whose class pydicom can rebuild from one string (a
# `TypeError` for `str` under OB) and for every refusal inside a sequence
# item (the issue's case: an icon label the file cannot encode). No
# patient data is in a traceback; the path is.
#
# `_export_failure_reason` is the one spelling of a worker's failure now:
# pydicom's sentence, which names the element, up to where the traceback
# starts.

TRACEBACK_WORDS = ("Traceback", "site-packages", 'File "', ".py", "line ")


def _refused_by_pydicoms_writer(ds):
    """The exception `ds` raises from pydicom's writer, written the way
    the exporter writes an uncompressed file: implicit VR, little endian."""
    import io
    import pydicom

    with pytest.raises(Exception) as raised:
        pydicom.dcmwrite(io.BytesIO(), ds, implicit_vr=True,
                         little_endian=True)
    return raised.value


def _text_under_ob(nested=False):
    """A dataset holding `str` under OB, at the top level or in an icon."""
    from pydicom.dataset import Dataset
    from pydicom.sequence import Sequence

    holder = Dataset()
    holder.add_new(0x00420011, "OB", "not bytes")
    if not nested:
        return holder
    ds = Dataset()
    ds.IconImageSequence = Sequence([holder])
    return ds


def _root_words(exc):
    """The innermost exception's own message: the interpreter's words."""
    while exc.__cause__ is not None:
        exc = exc.__cause__
    return str(exc)


def test_the_premise_pydicoms_writer_puts_a_traceback_in_the_message():
    """If pydicom stops doing this, the tests below pin nothing: say so."""
    exc = _refused_by_pydicoms_writer(_text_under_ob())

    assert type(exc) is TypeError
    assert str(exc).startswith(
        f"With tag (0042,0011) got exception: {_root_words(exc)}\n"
        f"Traceback (most recent call last):\n"), str(exc)
    assert "site-packages" in str(exc) or "pydicom" in str(exc)


@pytest.mark.parametrize("nested, sentence", [
    (False, "With tag (0042,0011) got exception: "),
    (True, "With tag (0088,0200) got exception: "
           "With tag (0042,0011) got exception: ")],
    ids=["top-level", "in-a-sequence-item"])
def test_a_writer_refusal_is_spelled_without_its_traceback(nested, sentence):
    """The class, pydicom's sentence naming each element, and the reason.

    In a sequence item the exception's cause is the item's own re-raise,
    whose message holds a traceback too, so cutting the message alone
    would leave one in a `(caused by ...)` tail. There is no tail: the
    cause is the exception the message already quotes. That also makes
    the text the same whether the exception crossed a process boundary
    (which drops `__cause__`) or not. Killing mutations: (t1) the cut
    removed; (t2) the cause appended as `describe_exception` appends it.
    """
    from isocenter.io_handlers import _export_failure_reason

    exc = _refused_by_pydicoms_writer(_text_under_ob(nested))
    assert exc.__cause__ is not None
    reason = _export_failure_reason(exc)

    assert reason == f"TypeError: {sentence}{_root_words(exc)}"
    for word in TRACEBACK_WORDS + ("caused by",):
        assert word not in reason, reason


def test_a_traceback_in_the_cause_alone_is_cut_there():
    """Our own sentence raised `from` a writer refusal keeps its cause.

    Killing mutation (t3): only the exception's own message is cut.
    """
    from isocenter.io_handlers import _export_failure_reason

    inner = _refused_by_pydicoms_writer(_text_under_ob())
    try:
        raise RuntimeError("the file could not be written") from inner
    except RuntimeError as exc:
        reason = _export_failure_reason(exc)

    assert reason == (
        "RuntimeError: the file could not be written (caused by TypeError: "
        f"With tag (0042,0011) got exception: {_root_words(inner)})")


@pytest.mark.parametrize("exc", [
    TypeError("a bytes-like object is required, not 'str'"),
    RuntimeError("Compression failed: Traceback is a word here too"),
    KeyError(),
    PermissionError(13, "Permission denied", "/out/Subject_P1/1.2.dcm"),
    OSError("cannot open /out/Subject_P1/r.dat")])
def test_a_reason_with_no_traceback_is_spelled_as_before(exc):
    """A no-op wherever pydicom embedded nothing: no existing row moves.

    Killing mutation (t4): the cut made at the bare word `Traceback`.
    """
    from isocenter.io_handlers import _export_failure_reason
    from isocenter.logger import describe_exception_without_paths

    assert _export_failure_reason(exc) == describe_exception_without_paths(exc)


def test_a_chained_reason_with_no_traceback_is_spelled_as_before():
    from isocenter.io_handlers import _export_failure_reason

    try:
        try:
            raise KeyError("x")
        except KeyError as inner:
            raise RuntimeError("channel table is malformed") from inner
    except RuntimeError as exc:
        assert _export_failure_reason(exc) == (
            "RuntimeError: channel table is malformed (caused by KeyError: 'x')")


def test_the_workers_console_line_carries_no_traceback(tmp_path, capfd):
    """The worker's stderr line is spelled by the same helper.

    Killing mutation (t5): the console line left on
    `describe_exception_without_paths`.
    """
    uid = "1.2.826.0.1.1053"
    inst = _ct_instance(uid)
    inst.set_attr("0042,0011", "not bytes")
    capfd.readouterr()

    outcome = _export_instance_worker(ExportContext(
        instance=inst, output_path=str(tmp_path / "out" / f"{uid}.dcm"),
        patient_attributes={"0010,0010": "ANON", "0010,0020": "P123"},
        study_attributes={"0020,000d": "1.2.826.0.2.1053"},
        series_attributes={"0020,000e": "1.2.826.0.3.1053"}))
    err = capfd.readouterr().err

    assert outcome.ok is False and type(outcome.error) is TypeError, outcome
    lines = [line for line in err.splitlines() if "Export failed" in line]
    assert lines == [
        f"ERROR: Export failed for instance {uid}: TypeError: With tag "
        f"(0042,0011) got exception: {_root_words(outcome.error)}"], err
    for word in TRACEBACK_WORDS:
        assert word not in err, err


def _icon_session(tmp_path, edit):
    """A session over one CT with an icon, `edit` applied to the instance."""
    from isocenter.session import DicomSession
    from tests.test_export_photometric_admissibility import _icon_source

    src = tmp_path / "src"
    src.mkdir()
    _icon_source(str(src), samples=3)
    session = DicomSession(str(tmp_path / "s.db"))
    session.ingest(str(src))
    inst = session.store.patients[0].studies[0].series[0].instances[0]
    edit(inst)
    return session, inst.sop_instance_uid


@pytest.mark.parametrize("edit, sentence, reason", [
    # The issue's case: a label the file's character set cannot encode.
    # pydicom cannot rebuild the `UnicodeEncodeError` from one string, so
    # the reason is the interpreter's `TypeError` for that; its words are
    # not pinned (they are CPython's), the absence of a traceback is.
    (lambda inst: inst.sequences["0088,0200"].items[0].set_attr(
        "0028,0004", "ＲＧＢ"),
     "TypeError: With tag (0088,0200) got exception: ", None),
    (lambda inst: inst.set_attr("0042,0011", "not bytes"),
     "TypeError: With tag (0042,0011) got exception: ", "words"),
    (lambda inst: inst.sequences["0088,0200"].items[0].set_attr(
        "0042,0011", "not bytes"),
     "TypeError: With tag (0088,0200) got exception: "
     "With tag (0042,0011) got exception: ", "words")],
    ids=["icon-label-not-encodable", "text-under-ob", "text-under-ob-nested"])
def test_no_error_row_and_no_export_error_carries_a_traceback(
        tmp_path, edit, sentence, reason):
    """The row as stored, `ExportError`, its failures and the report (#1053).

    Measured before, for each of the three: the row, `ExportError`'s
    message and the report held `Traceback (most recent call last): File
    "/Users/.../site-packages/pydicom/tag.py", line 29, in
    tag_in_exception ...` through to the last frame. Killing mutation
    (t6): `_report_export_failures` left on
    `describe_exception_without_paths`.
    """
    session, uid = _icon_session(tmp_path, edit)
    out = tmp_path / "out"
    report = tmp_path / "report.md"
    try:
        with pytest.raises(ExportError) as raised:
            session.export(str(out), show_progress=False)
        rows = _rows(session, "ERROR")
        session.generate_report(str(report))
    finally:
        session.close()

    assert [u for u, _d in rows] == [uid], rows
    detail = rows[0][1]
    opening = f"Export failed for instance {uid}: {sentence}"
    assert detail.startswith(opening), detail
    if reason is not None:
        words = _root_words(_refused_by_pydicoms_writer(_text_under_ob()))
        assert detail == opening + words, detail
    assert raised.value.failures == [(uid, detail)]
    message = str(raised.value).replace(str(out), "<folder>")
    assert message == (
        f"Export to <folder> wrote 0 of 1 planned instances; 1 failed and "
        f"none is reported as written. First: {uid}: {detail}. See the "
        f"audit log for the rest."), message
    text = report.read_text(encoding="utf-8")
    assert detail in text, "the ERROR row did not reach the report"
    for word in TRACEBACK_WORDS + ("/",):
        assert word not in detail, (word, detail)
        assert word not in message, (word, message)
    for word in ("Traceback", "site-packages", "tag_in_exception"):
        assert word not in text, word


def test_a_bare_exception_in_the_results_is_spelled_by_the_same_helper():
    """The arm for a result that is an exception, not an outcome.

    That arm is where a lost worker's exception arrives, but this is not
    one: a lost worker arrives as a `BrokenProcessPool`, whose message
    holds no traceback. The test hands the arm an exception that does,
    pydicom's, which is the only way to see which helper spells it.
    Killing mutation (t8): the arm left on
    `describe_exception_without_paths`.
    """
    from isocenter.io_handlers import DicomExporter

    lost = _refused_by_pydicoms_writer(_text_under_ob())
    failures = DicomExporter._report_export_failures([lost])

    assert failures == [("UNKNOWN", (
        "Export worker failed: TypeError: With tag (0042,0011) got "
        f"exception: {_root_words(lost)}"))]


def test_write_trees_error_carries_no_traceback(tmp_path):
    """`write_tree()` raises on the first failure, quoting its message.

    The serializer's `RuntimeError` is no row and no `ExportError`, and it
    quoted the same message whole. Its text is otherwise as before (the
    message with no class); only the traceback is cut. Killing mutation
    (t7): `First error:` left quoting the message whole.
    """
    from datetime import date

    from isocenter.entities import Patient, Series, Study
    from isocenter.io_handlers import DicomExporter

    inst = _ct_instance("1.2.826.0.1.1053.2")
    inst.set_attr("0042,0011", "not bytes")
    patient = Patient("PAT1", "Original Name")
    study = Study("1.2.826.0.2.1053", date(2023, 1, 1))
    study.study_time = "120000"
    series = Series("1.2.826.0.3.1053", "CT", 1)
    series.instances.append(inst)
    study.series.append(series)
    patient.studies.append(study)

    with pytest.raises(RuntimeError) as raised:
        DicomExporter.write_tree(patient, str(tmp_path / "out"),
                                 compression=None, show_progress=False)

    words = _root_words(_refused_by_pydicoms_writer(_text_under_ob()))
    assert str(raised.value) == (
        "Export incomplete. 1 failed. First error: With tag (0042,0011) "
        f"got exception: {words}")

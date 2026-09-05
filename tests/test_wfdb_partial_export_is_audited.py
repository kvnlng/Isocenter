"""A WFDB export that lost records must not grade PASS (#332).

`WfdbExporter.export` contains each instance's failure the way the DICOM
exporter does -- catch, log, continue -- so one malformed instance out of
hundreds cannot abort the run and leave every later patient silently
unexported. Containment is right. What was missing is the other half of
the pattern: the DICOM path records an `ERROR` audit row per failure
(`_report_export_failures`, #126/#181), and the WFDB path recorded none.

The consequence was a run that lost records and could not say so.
`get_audit_errors()` found nothing, the compliance report's "Exceptions &
Errors" section was empty, and the grade read `PASS` -- for an export
that wrote eight of ten waveforms. A returned list one entry short is not
a channel: nothing reads its length against the graph, and the whole
point of #181 was that a caller who did not compare could not tell.

**`ERROR` is the existing vocabulary, not a new one.** It is what
`DicomExporter._report_export_failures` writes, what `get_audit_errors()`
selects, and what the report renders and grades on. This exporter already
writes audit rows through that channel (its `DATA_LOSS` rows for dropped
multiplex groups), and `store_backend` is already threaded down to the
loop. So the fix is a row, not plumbing -- and deliberately not a change
to what `export()` returns: `session.export(format="wfdb")` returns
`List[str]` consumed positionally at ~25 sites, and an attempted/written
count would be a second result shape for a question the audit log
already answers (#191 declined the format-dependent return).

The second test is the vacuity floor. Without it the new row could be a
constant that every export writes, which would grade a clean run
`REVIEW_REQUIRED` and teach its readers to skip the section.
"""
import sqlite3

import pytest

from isocenter.exporters.wfdb import WfdbExporter
from isocenter.session import DicomSession
from scripts.generate_waveform_test_data import write_fixture


def _session_with_two_ecgs(tmp_path, name="wfdb_partial.db"):
    src = tmp_path / "src"
    src.mkdir()
    write_fixture(str(src / "a.dcm"), num_samples=64, patient_id="WF_A",
                  patient_name="Alpha^Test")
    write_fixture(str(src / "b.dcm"), num_samples=64, patient_id="WF_B",
                  patient_name="Beta^Test")

    session = DicomSession(persistence_file=str(tmp_path / name))
    session.ingest(str(src))
    # So the clean run below grades on its own merits: an un-anonymized
    # session grades REVIEW_REQUIRED for a reason that has nothing to do
    # with this exporter, and the floor test would then pass while
    # measuring nothing (the setup `tests/test_report_export_boundary.py`
    # records for the same trap).
    session.anonymize()
    return session


def _instances(session):
    return [i for p in session.store.patients for st in p.studies
            for se in st.series for i in se.instances]


def _rows(session, action_type):
    """Audit rows of one type, read through the barrier.

    `flush_audit_queue()` first, always. `log_audit` enqueues, and the
    writer thread drains on its own schedule, so a bare SELECT is a race
    that answers `[]` for a store with the row recorded -- and `[]` is
    also the unfixed answer, which would make every red here
    indistinguishable from a timing artefact.
    """
    session.store_backend.flush_audit_queue()
    with sqlite3.connect(session.persistence_file) as conn:
        return conn.execute(
            "SELECT entity_uid, details FROM audit_log "
            "WHERE action_type=?", (action_type,)).fetchall()


def _grade(text):
    for line in text.splitlines():
        if "Validation Status" in line:
            return line
    raise AssertionError(f"no Validation Status line in report:\n{text}")


def _report_text(session, tmp_path, name):
    path = str(tmp_path / name)
    session.generate_report(path)
    with open(path, encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def broken_first_instance(tmp_path, monkeypatch):
    """One of two waveforms fails to write; the other must still land.

    The failure is injected at `_write_instance` rather than by
    corrupting a fixture, so the *containment* the exporter already has
    is exercised exactly as it is -- the exception escapes the write and
    is caught by the loop's own `except`, which is the code under test.
    """
    session = _session_with_two_ecgs(tmp_path)
    doomed = _instances(session)[0].sop_instance_uid
    real_write = WfdbExporter._write_instance

    def failing(self, folder, patient, study, series, instance, *args,
                **kwargs):
        if instance.sop_instance_uid == doomed:
            raise RuntimeError("waveform channel table is malformed")
        return real_write(self, folder, patient, study, series, instance,
                          *args, **kwargs)

    monkeypatch.setattr(WfdbExporter, "_write_instance", failing)
    try:
        yield session, doomed
    finally:
        session.close()


def test_a_swallowed_wfdb_failure_is_recorded_as_an_error_row(
        broken_first_instance, tmp_path):
    """The row the DICOM path has always written, for this format (#332)."""
    session, doomed = broken_first_instance

    written = session.export(str(tmp_path / "out"), format="wfdb")

    # #330's behaviour, restated as a precondition rather than assumed:
    # the failure is still *contained*. A fix that let it escape would
    # abort the run and lose the record that did write.
    assert len(written) == 1, (
        "the failure was no longer contained: one bad instance must not "
        f"abort the run, and did. Wrote: {written}")

    errors = _rows(session, "ERROR")
    assert any(uid == doomed for uid, _ in errors), (
        "the WFDB exporter swallowed a per-instance failure without an "
        "ERROR audit row, so get_audit_errors() finds nothing, the "
        "report's Exceptions & Errors section is empty, and a run that "
        f"lost a record grades PASS (#332). ERROR rows: {errors}")

    detail = next(d for uid, d in errors if uid == doomed)
    assert "malformed" in detail, (
        f"the row does not carry the reason the write failed: {detail!r}")
    assert "\n" not in detail and "|" not in detail, (
        "the detail is rendered straight into a markdown table row and "
        f"must be flattened and pipe-escaped first: {detail!r}")

    exports = _rows(session, "EXPORT")
    assert len(exports) == 1, f"expected one EXPORT row, got {exports}"
    export_detail = exports[0][1]
    assert "1 record" in export_detail and "1 instance" in export_detail, (
        "the EXPORT row names only what was written, so the whole-run "
        "question -- 'it says 1, was it 1 of 1 or 1 of 2?' -- still has "
        f"no answer at the level a reader looks first: {export_detail!r}")

    text = _report_text(session, tmp_path, "partial.md")
    assert "REVIEW_REQUIRED" in _grade(text), (
        "a run that lost a waveform graded clean; the grade is the only "
        f"thing most readers look at (#332).\n{text}")
    assert doomed in text, (
        "the report grades the failure but does not name the record it "
        f"lost, so nobody can act on it.\n{text}")


def test_a_clean_wfdb_export_writes_no_error_row(tmp_path):
    """The new row must not be a constant (#332).

    A row every export writes would grade every clean run
    `REVIEW_REQUIRED`, and a section that is never empty is one its
    readers learn to skip -- which is the same silence #332 is about,
    approached from the other side.
    """
    session = _session_with_two_ecgs(tmp_path, name="wfdb_clean.db")
    try:
        written = session.export(str(tmp_path / "out"), format="wfdb")
        assert len(written) == 2, (
            f"the clean export did not write both records: {written}")

        assert _rows(session, "ERROR") == [], (
            "a clean export recorded an export failure; the row is a "
            "constant rather than a report of something that happened")

        exports = _rows(session, "EXPORT")
        assert len(exports) == 1, f"expected one EXPORT row, got {exports}"
        assert "0 instance" in exports[0][1], (
            "the EXPORT row does not say that nothing failed; a count "
            "that appears only on the bad path cannot be read as zero "
            f"on the good one: {exports[0][1]!r}")

        text = _report_text(session, tmp_path, "clean.md")
        assert "PASS" in _grade(text), (
            f"a clean WFDB export no longer grades PASS.\n{text}")
    finally:
        session.close()


def test_a_failure_with_no_store_behind_it_does_not_raise(tmp_path,
                                                          monkeypatch):
    """`_write_instance` is called directly, with `store_backend=None`.

    The split `io_handlers.py` already makes for `write_tree()`: the log
    line is unconditional, and the audit row is what a store adds. A fix
    that wrote the row without checking would turn every session-less
    call -- the fixture generators in `scripts/`, and the exporter's own
    unit tests -- into an `AttributeError` on `None`.
    """
    session = _session_with_two_ecgs(tmp_path, name="wfdb_nostore.db")
    try:
        exporter = WfdbExporter()

        def failing(self, *args, **kwargs):
            raise RuntimeError("no store here")

        monkeypatch.setattr(WfdbExporter, "_write_instance", failing)
        monkeypatch.setattr(session, "store_backend", None, raising=False)

        written = exporter.export(session, str(tmp_path / "out"))

        assert written == [], (
            f"nothing should have been written: {written}")
    finally:
        session.store_backend = None
        session.persistence_manager.shutdown()


def test_an_instance_with_no_uid_is_still_named_in_the_row(tmp_path,
                                                           monkeypatch):
    """`sop_instance_uid` can be `None`, and a row must still identify one.

    `_report_export_failures` falls back for exactly this reason
    (`r.sop_instance_uid or r.output_path`, else `"UNKNOWN"`). A row
    whose `entity_uid` is `None` is a failure nobody can look up, and
    sqlite stores the `None` without complaint.

    **The UIDs are put back before `close()`**, and that is not tidiness.
    `Session._warn_about_unsaved_instances` joins them into a message
    with `", ".join(...)` and raises `TypeError: sequence item 0:
    expected str instance, NoneType found` on a `None` -- so a test that
    left them unset would fail in teardown, on a defect that has nothing
    to do with this exporter, and the red above would be unreadable.
    That crash is real and is reported separately; it is not #332's.
    """
    session = _session_with_two_ecgs(tmp_path, name="wfdb_nouid.db")
    instances = _instances(session)
    original = [i.sop_instance_uid for i in instances]
    try:
        for instance in instances:
            instance.sop_instance_uid = None

        def failing(self, *args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(WfdbExporter, "_write_instance", failing)
        session.export(str(tmp_path / "out"), format="wfdb")

        errors = _rows(session, "ERROR")
        assert errors, "no ERROR row was written at all"
        assert all(uid for uid, _ in errors), (
            "a failure was recorded under a NULL entity_uid, so the row "
            f"names nothing a reader could look up: {errors}")
    finally:
        for instance, uid in zip(instances, original):
            instance.sop_instance_uid = uid
        session.close()

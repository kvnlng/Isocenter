"""What `session.export()` promises about its own parameters and output.

`_export_dicom` was a single 306-line method with 45 branches: the
pre-export safety scan, its console report, the suggested-config block,
subset resolution, the export-plan walk and the parallel batch, all in
one body. These tests pin the parts a caller can observe, so the split
is checkable rather than hopeful.

Two of them describe parameters that the method accepted and then
ignored -- the failure mode this file exists to catch, because a
silently-discarded argument looks exactly like a working one.
"""
import datetime

import pytest

from isocenter import io_handlers
from isocenter.io_handlers import ExportSummary
from isocenter.builders import DicomBuilder
from isocenter.session import DicomSession


@pytest.fixture
def session_with_phi(tmp_path):
    """One patient carrying an obvious identifier, so a scan finds something."""
    session = DicomSession(str(tmp_path / "session"))
    patient = (DicomBuilder.start_patient("P123", "John Doe")
               .add_study("S1", datetime.date(2023, 1, 1))
               .add_series("SE1", "CT", 1)
               .add_instance("I1", "1.2.3", 1)
               .end_instance().end_series().end_study().build())
    session.store.patients.append(patient)
    yield session
    session.close()


@pytest.fixture
def captured_batch(monkeypatch):
    """Intercepts the parallel export so tests can read what it was asked for."""
    calls = []

    def record(tasks, **kwargs):
        calls.append({"tasks": list(tasks), **kwargs})
        # The real `export_batch` returns an `ExportSummary`, and the
        # caller reads `written` off it to report what was delivered
        # (#181). A bare count here would pass this fixture and fail
        # every caller.
        return ExportSummary(
            written_uids=[ctx.instance.sop_instance_uid
                          for ctx in calls[-1]["tasks"]])

    monkeypatch.setattr(io_handlers.DicomExporter, "export_batch",
                        staticmethod(record))
    return calls


def test_show_progress_false_is_honoured(session_with_phi, captured_batch,
                                         tmp_path):
    """`show_progress=False` must reach the batch runner.

    The parameter was accepted, documented, and then overwritten by a
    bare `show_progress = True` three lines before it was used, so every
    export drew a progress bar whichever way it was called. Four tests in
    this suite pass `show_progress=False` and none of them noticed.
    """
    session_with_phi.export(str(tmp_path / "out"), show_progress=False)

    assert captured_batch, "the export never reached the batch runner"
    assert captured_batch[0]["show_progress"] is False


def test_the_suggested_config_block_is_printed_once(session_with_phi,
                                                    tmp_path, capsys):
    """The scan's config suggestion is emitted once, not twice.

    A duplicated pair of `print` calls used to emit the block's closing
    lines again, so the snippet the report tells the user to copy was not
    well-formed even on its own terms. Asserted through the section key
    rather than a closing brace: the fragment is YAML since #20, and a
    test that pins the punctuation of one format cannot outlive it.
    """
    session_with_phi.export(str(tmp_path / "out"), check_burned_in=True)

    stdout = capsys.readouterr().out

    assert stdout.count("Suggested Config Update:") == 1
    assert stdout.count("phi_tags:") == 1, (
        "the suggested-config block was emitted more than once")


def test_the_safety_scan_names_what_it_found_without_borrowing_the_word_dirty(
        session_with_phi, tmp_path, capsys):
    """The report says identifiers, not "dirty".

    Persistence state already answers a different question about every
    entity in the session: `has_unsaved_changes`. Using "dirty" for
    "still carries PHI" in user-facing output made the two
    indistinguishable to a reader who had seen either one.
    """
    session_with_phi.export(str(tmp_path / "out"), check_burned_in=True)

    stdout = capsys.readouterr().out

    assert "Safety Scan Found Issues" in stdout
    assert "0010,0010" in stdout and "John Doe" in stdout
    assert "dirty" not in stdout.lower(), (
        "the PHI report uses the vocabulary of the persistence layer")


def test_safe_export_skips_an_instance_whose_patient_carries_phi(
        session_with_phi, captured_batch, tmp_path):
    """The safety filter is hierarchical: a flagged parent excludes children."""
    session_with_phi.export(str(tmp_path / "out"), check_burned_in=True)

    exported = [ctx.instance.sop_instance_uid
                for call in captured_batch for ctx in call["tasks"]]
    assert "I1" not in exported


def test_an_export_plan_carries_the_configured_redaction_zones(
        session_with_phi, captured_batch, tmp_path):
    """Zones come from the rule matching the series' device serial number."""
    from isocenter.entities import Equipment

    series = session_with_phi.store.patients[0].studies[0].series[0]
    series.equipment = Equipment("GE", "Revolution CT", "SN-ZONES")
    session_with_phi.configuration.rules = [{
        "serial_number": "SN-ZONES",
        "redaction_zones": [[0, 0, 10, 10]],
    }]

    session_with_phi.export(str(tmp_path / "out"))

    contexts = [ctx for call in captured_batch for ctx in call["tasks"]]
    assert contexts, "nothing was queued for export"
    assert contexts[0].redaction_zones == [[0, 0, 10, 10]]


def test_an_unusable_subset_is_refused_rather_than_ignored(
        session_with_phi, captured_batch, tmp_path):
    """A subset the method cannot interpret must not become a full export.

    Anything that was not a string, DataFrame or list fell through every
    branch leaving the filter as None -- so a caller who asked to export
    one series, and got the argument's type wrong, exported the entire
    cohort instead. Over-exporting is the dangerous direction for a
    de-identification tool to fail in.
    """
    with pytest.raises(TypeError, match="subset must be"):
        session_with_phi.export(str(tmp_path / "out"), subset=42)

    assert not captured_batch, "an unusable subset still exported something"


def test_a_broken_subset_query_is_reported(session_with_phi, tmp_path):
    """A query that does not run raises instead of exporting nothing quietly.

    The old path logged the pandas error and returned, so a mistyped
    query and a query that legitimately matched no rows produced the
    same result: an empty output directory and a zero exit.
    """
    with pytest.raises(ValueError, match="could not be run"):
        session_with_phi.export(str(tmp_path / "out"),
                                subset="NoSuchColumn == 'x'")


# ---------------------------------------------------------------------
# What `export()` hands back, and when it refuses to hand anything back
# (#191). `_export_dicom` returned `None`, so a caller had no way to
# tell an export that wrote every file from one that wrote none: the
# failures were audited and graded, and the *call* looked identical.
# ---------------------------------------------------------------------

def _exported_files(folder):
    return sorted(p for p in folder.rglob("*.dcm"))


def test_a_clean_export_returns_a_summary_naming_what_it_wrote(tmp_path):
    """The ordinary case: a summary, not `None`."""
    from tests.test_export_failure_audit import _session

    session = _session(tmp_path)
    try:
        summary = session.export(str(tmp_path / "out"), show_progress=False)
    finally:
        session.close()

    assert isinstance(summary, ExportSummary)
    assert summary.written == 3
    assert summary.failures == []


def test_a_partial_export_returns_its_summary_and_does_not_raise(tmp_path):
    """A partial export is a real, usable result.

    Rejected: raising on *any* failure. Two instances reached disk and
    the caller may well want them; raising would discard the summary
    that says which, and would have to decide the fate of the files
    already written.
    """
    from tests.test_export_failure_audit import _session

    session = _session(tmp_path, break_instances=(1,))
    try:
        summary = session.export(str(tmp_path / "out"), show_progress=False)
    finally:
        session.close()

    assert summary.written == 2
    assert summary.failed == 1


def test_an_export_that_wrote_nothing_raises_after_recording_everything(
        tmp_path):
    """Zero of three planned instances is a failed export, and now says so.

    The raise is **last**, after all five records, exactly as
    `_apply_redaction_rules` raises `RedactionError`: a caller who
    catches this still holds a correct graph, a complete audit trail and
    a report that grades `REVIEW_REQUIRED`. All five are asserted here,
    because "raises" would be a poor trade for any of them.
    """
    from isocenter.io_handlers import ExportError
    from tests.test_export_failure_audit import _audit, _session

    session = _session(tmp_path, break_instances=(0, 1, 2))
    out = tmp_path / "out"
    report = tmp_path / "report.md"
    try:
        db_path = session.store_backend.db_path
        with pytest.raises(ExportError) as caught:
            session.export(str(out), show_progress=False)

        assert caught.value.attempted == 3
        assert len(caught.value.failures) == 3

        session.generate_report(str(report))
        content = report.read_text(encoding="utf-8")
        errors = _audit(db_path, "ERROR")
        exports = _audit(db_path, "EXPORT")
    finally:
        session.close()

    assert _exported_files(out) == [], "nothing should have reached disk"
    assert len(errors) == 3, "one ERROR row per instance that did not write"
    assert len(exports) == 1, "the run is still an audited action (#166)"
    assert "Instances Written | 0 of 3 requested" in content
    assert "**REVIEW_REQUIRED**" in content


def test_an_empty_plan_returns_an_empty_summary_and_does_not_raise(tmp_path):
    """Zero of zero is a plan that matched nothing, not a failed export.

    The guard is `written == 0 **and** failures`, so this path cannot
    raise however it is reached. The `EXPORT` audit row above the early
    return already says the subset matched nothing, and the report's
    export boundary keys on that row existing (#153, #166).
    """
    from tests.test_export_failure_audit import _audit, _session

    session = _session(tmp_path)
    try:
        db_path = session.store_backend.db_path
        summary = session.export(str(tmp_path / "out"), show_progress=False,
                                 subset=["1.2.826.0.1.no-such-instance"])
        # The row is written asynchronously; `_audit` opens its own
        # sqlite connection and would read the table before the audit
        # thread reaches it. Every audit *reader* on `SqliteStore`
        # flushes for this reason -- a raw query has to do it by hand.
        session.store_backend.flush_audit_queue()
        exports = _audit(db_path, "EXPORT")
    finally:
        session.close()

    assert isinstance(summary, ExportSummary)
    assert summary.written == 0 and summary.failures == []
    assert len(exports) == 1


def test_export_error_is_a_runtime_error_and_importable_from_the_package():
    """`except RuntimeError` around a full run must keep catching this.

    Same argument `RedactionError` makes for itself: `write_tree` and
    `_export_instance_worker` already raise bare `RuntimeError`s on this
    pipeline, so subclassing keeps every existing handler working where
    subclassing `Exception` would turn a caught error into an escaping
    one. And an exception a caller is expected to catch needs a stable
    import path -- `isocenter.io_handlers` is not one this package
    advertises.
    """
    import isocenter
    from isocenter.io_handlers import ExportError

    assert issubclass(ExportError, RuntimeError)
    assert isocenter.ExportError is ExportError
    assert "ExportError" in isocenter.__all__


def test_a_caught_export_error_still_closes_without_unsaved_instances(
        tmp_path, capsys):
    """Where #191 and #307 meet: a raising export must still close clean.

    `_export_dicom` saves before the batch and nothing after it mutates
    the graph, so a caught `ExportError` leaves nothing unsaved. Asserted
    on the dirty list itself as well as on the absence of the warning --
    the absence alone would pass against a graph that was dirty and a
    warning that had been deleted.
    """
    from isocenter.io_handlers import ExportError
    from tests.test_export_failure_audit import _session

    session = _session(tmp_path, break_instances=(0, 1, 2))
    try:
        with pytest.raises(ExportError):
            session.export(str(tmp_path / "out"), show_progress=False)

        dirty = [inst
                 for p in session.store.patients
                 for st in p.studies
                 for se in st.series
                 for inst in se.instances
                 if inst.has_unsaved_changes]
        assert dirty == [], (
            "a caught ExportError left instances unsaved, so close() "
            "reports a loss the export did not cause")

        capsys.readouterr()
    finally:
        session.close()

    assert "unsaved change" not in capsys.readouterr().out

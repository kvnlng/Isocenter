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

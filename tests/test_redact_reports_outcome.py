"""`session.redact()` has to say what it did, including when it failed.

Redaction is the operation that removes burned-in PHI from pixels. Its
entire body was wrapped in `except Exception`, which logged, printed, and
returned normally -- so a redaction that removed nothing was
indistinguishable from one that removed everything, to a caller and to a
script. On a de-identification tool that is the worst shape a failure can
take: the step that was supposed to remove PHI did not, and the pipeline
moves on as though it had.

These tests state what a caller can rely on: a count on success, an
exception on failure, a warning when only some tasks landed, and console
output that does not contradict itself.
"""
import os

import numpy as np
import pytest

from isocenter.entities import Patient, Study, Series, Instance, Equipment
from isocenter.session import DicomSession


@pytest.fixture
def session(tmp_path):
    """A session with three redactable instances on one configured machine."""
    sess = DicomSession(persistence_file=str(tmp_path / "redact.db"))

    patient = Patient("P1", "Test^Patient")
    study = Study("S1", "20230101")
    series = Series("SE1", "OT", 1)
    series.equipment = Equipment("Acme", "Scanner", "SERIAL_1")

    for i in range(3):
        inst = Instance(f"I{i}", "1.2.840.10008.5.1.4.1.1.2", i + 1)
        inst.set_pixel_data(np.full((64, 64), 255, dtype=np.uint8))
        series.instances.append(inst)

    study.series.append(series)
    patient.studies.append(study)
    sess.store.patients.append(patient)

    sess.configuration.rules = [{
        "serial_number": "SERIAL_1",
        "redaction_zones": [[0, 0, 20, 20]],
    }]

    yield sess
    sess.close()


def test_redact_returns_how_many_instances_it_changed(session):
    """A caller needs a number, not None.

    Without one there is no way to assert in a pipeline that the
    redaction step did anything at all.
    """
    redacted = session.redact(show_progress=False)

    assert redacted == 3


def test_a_failing_redaction_raises_instead_of_reporting_completion(
        session, monkeypatch):
    """The step that removes PHI must not fail quietly.

    The old handler caught everything, printed `Execution interrupted`,
    and returned. A script checking for an exception saw success.
    """
    def explode(*_args, **_kwargs):
        raise RuntimeError("redaction backend unavailable")

    monkeypatch.setattr(session.configuration, "rules",
                        session.configuration.rules)
    monkeypatch.setattr(
        "isocenter.services.RedactionService.prepare_redaction_tasks", explode)

    with pytest.raises(RuntimeError, match="redaction backend unavailable"):
        session.redact(show_progress=False)


def test_no_rules_is_reported_as_nothing_done(session):
    """An unconfigured session redacts nothing, and says so with a number."""
    session.configuration.rules = []

    assert session.redact(show_progress=False) == 0


def test_the_console_does_not_claim_both_saved_and_unsaved(session, capsys):
    """Two unconditional prints contradicted each other.

    `"Remember to call .save() to persist."` was followed immediately by
    `"Execution Complete. Session saved."` -- neither conditional on
    anything. A user reading the console was told both.
    """
    session.redact(show_progress=False)
    out = capsys.readouterr().out

    assert "Session saved" not in out, (
        "redact() claims the session was saved; it was not")
    assert ".save()" in out, (
        "redact() should say the change is in memory until saved")


def test_redaction_does_not_print_debug_lines(session, capsys):
    """`DEBUG:` output was shipped, one or two lines per instance.

    On a cohort that is thousands of lines of internal detail on stdout,
    burying the safety-scan result the user is meant to read.
    """
    session.redact(show_progress=False)
    out = capsys.readouterr().out

    assert "DEBUG" not in out


def test_a_partially_applied_redaction_is_reported(session, monkeypatch,
                                                   caplog):
    """A task whose worker returned nothing must not vanish.

    `execute_redaction_task` returns None when it fails, and the result
    loop skipped falsy mutations without counting them -- so a run where
    two of three images failed to redact looked exactly like a clean one.

    Workers normally run in separate processes, where a monkeypatched
    failure cannot reach them. `run_parallel` is replaced with a serial
    map so the real worker and the real accounting run in-process; only
    the concurrency is stubbed out.

    The stub returns a benign no-mutation outcome rather than a bare
    `None`, because since #213 a `None` is a *failure* the parent audits
    and raises on -- not the "nothing to apply" it used to conflate with
    two other things. Both assertions below are unchanged; the test's
    meaning is exactly preserved, and is now stated in the vocabulary that
    distinguishes it from a failure.
    """
    import isocenter.services as services

    real = services.RedactionService.execute_redaction_task
    calls = {"n": 0}

    def fail_after_the_first(self, task):
        calls["n"] += 1
        if calls["n"] == 1:
            return real(self, task)
        return services.RedactionOutcome(
            ok=True, sop_instance_uid=task["instance"].sop_instance_uid,
            mutation=None)

    monkeypatch.setattr(services.RedactionService, "execute_redaction_task",
                        fail_after_the_first)
    monkeypatch.setattr("isocenter.session.run_parallel",
                        lambda fn, items, **_kwargs: [fn(i) for i in items])

    with caplog.at_level("WARNING"):
        redacted = session.redact(show_progress=False)

    assert redacted == 1
    assert any("1 of 3" in record.message for record in caplog.records), (
        "a partial redaction was not reported anywhere")


def test_a_malformed_worker_count_does_not_abort_the_redaction(
        session, monkeypatch):
    """A bad tuning variable must not silently cancel the whole operation.

    `int(os.environ["ISOCENTER_MAX_WORKERS"])` was unguarded, and its
    ValueError was caught by the same handler that swallowed everything
    else -- so a typo in a shell profile turned redaction into a no-op
    that reported success.
    """
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "banana")

    assert session.redact(show_progress=False) == 3

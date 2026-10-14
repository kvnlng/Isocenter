"""A `RedactionService` with no store backend refuses to redact (#1113).

Found in the review of #1111, measured on `main` at f31758d3: a
`RedactionService(store)` built with no backend, driven through
`redact_machine_instances`, gave the instance its redacted SOP Instance
UID, the redaction hash and Burned In Annotation `NO`, but with no backend
`persist_pixel_data` was never called, so the loader still pointed at the
original frame and the zeroed array was then discarded. A later export
wrote the redaction's UID and `NO` over the original pixels: a record of a
redaction that was not made.

Owner ruling of 2026-10-14: refuse, before any UID, flag or Burned In
Annotation is written. `Session.redact()`, `redact_by_machine()` and
`preview_config()` pass the backend and are not affected.
"""
import pytest

from isocenter.services import RedactionService
from isocenter.session import DicomSession
from support.project_secret import FIXED_A, load_fixed_secret
from support.serial_less_burned_cr import (INK_PIXELS, OVER, SERIAL, ink_left,
                                           source_uid, write_inputs)


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")


@pytest.fixture
def session(tmp_path):
    s = DicomSession(str(tmp_path / "s1113.db"))
    load_fixed_secret(s)
    s.ingest(write_inputs(str(tmp_path / "input"), ("serial",)))
    yield s
    s.close()


def _instance(session):
    return [i for p in session.store.patients for st in p.studies
            for se in st.series for i in se.instances][0]


def _untouched(inst):
    assert inst.sop_instance_uid == source_uid("serial")
    assert "_ISOCENTER_REDACTION_HASH" not in inst.attributes
    assert inst.attributes.get("0028,0301") != "NO"
    assert "DERIVED" not in str(inst.attributes.get("0008,0008"))
    assert ink_left(inst.get_pixel_data()) == INK_PIXELS


def test_the_direct_path_refuses_before_writing_anything(session):
    inst = _instance(session)
    with pytest.raises(RuntimeError, match="store backend"):
        RedactionService(session.store).redact_machine_instances(
            SERIAL, [tuple(OVER)], show_progress=False, project_secret=FIXED_A)
    _untouched(inst)


def test_the_rule_path_refuses_before_writing_anything(session):
    inst = _instance(session)
    with pytest.raises(RuntimeError, match="store backend"):
        RedactionService(session.store).process_machine_rules(
            {"serial_number": SERIAL, "redaction_zones": [list(OVER)]},
            show_progress=False, project_secret=FIXED_A)
    _untouched(inst)


def test_task_preparation_refuses(session):
    with pytest.raises(RuntimeError, match="store backend"):
        RedactionService(session.store).prepare_redaction_tasks(
            {"serial_number": SERIAL, "redaction_zones": [list(OVER)]},
            project_secret=FIXED_A)
    _untouched(_instance(session))


def test_a_task_run_by_a_service_with_no_backend_fails_untouched(session):
    """A task prepared by a backed service, run by one with none: the
    outcome is a failure, and nothing was written on the instance."""
    backed = RedactionService(session.store, session.store_backend)
    task = backed.prepare_redaction_tasks(
        {"serial_number": SERIAL, "redaction_zones": [list(OVER)]},
        project_secret=FIXED_A)[0]
    outcome = RedactionService(session.store).execute_redaction_task(task)
    assert outcome.ok is False
    assert "store backend" in outcome.error
    _untouched(_instance(session))


def test_session_redact_still_redacts(session):
    """The public verb passes its backend: unaffected."""
    session.configuration.rules = [
        {"serial_number": SERIAL, "redaction_zones": [list(OVER)]}]
    assert session.redact(show_progress=False) == 1
    assert ink_left(_instance(session).get_pixel_data()) == 0

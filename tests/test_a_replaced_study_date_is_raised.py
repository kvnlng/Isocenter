"""A study date the pipeline did not produce is raised (#518).

`PhiInspector._scan_study` returned no findings at all while
`study.date_shifted` was set, and that flag records *that* a shift
happened, never *what it produced* -- so it could not tell its own output
from a new input. Measured on `927cb2b` on 3.12.14 (threads and
processes) and 3.14.7t: shift a study's date (`2023-01-01` ->
`2022-03-21`), assign `study.study_date = date(2024, 7, 4)`, re-audit --
`raised=0`, `date_shifted=True`, and the fresh original is exported.

The same flag made an instance's top-level copy of that fresh date skip
as "the owner's replacement" in `_holds_owners_replacement` (#496), so
#518 was reachable through the instance scan too. Both now ask
`privacy._study_date_is_this_pipelines`, spelled once: leaving either on
the flag keeps half of this defect alive.

**The rule these tests hold.** Same root as #510 and #513, one level up,
and the same shape of answer: `Study._shifted_study_date` holds what the
shift produced, as the DA string `io_handlers.format_study_date` renders
(#189, the one spelling of a Study's date as a string, so a `date` and
its string cannot disagree). A study whose `study_date` is that value is
not raised; anything else is. `Study.date_shifted` is **kept** -- it
answers the honest entity-level question ("did a de-identifying shift run
on this study?"), it is what the WFDB header's `# de-identified start
date:` comment is derived from, and read *with* the record it is what
tells a pre-0.9.6 row from a fresh one.

`False`/NULL means never shifted; `True`/string means shifted by 0.9.6 or
later and this is what it produced; `True`/NULL means shifted before
0.9.6, value unknowable, and keeps the pre-0.9.6 rule (the owner's
ruling, covered in `tests/test_a_pre_096_store_is_not_reshifted.py`);
`False`/string is a hand-edited or partially-written row and the record
wins, being the more specific claim.

**Why this file imports what it does.** It reaches `_scan_study` and
`_holds_owners_replacement` through `isocenter.privacy`, the record
through `isocenter.entities`, the arm through `isocenter.remediation`,
the clone through `isocenter.session` and the round trip through
`isocenter.persistence`; see `test_mutation_probe_targets.py`.
"""
import sqlite3
from datetime import date

import pytest

from isocenter.entities import Instance, Patient, Series, Study
from isocenter.io_handlers import format_study_date
from isocenter.privacy import PhiInspector, _study_date_is_this_pipelines
from isocenter.session import DicomSession

from support.project_secret import FIXED_A, load_fixed_secret

SC_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.7"
STUDY_DATE = "0008,0020"
STUDY_UID = "1.2.826.0.1.518"
PID = "P1"

#: `P1`'s offset with the default jitter config under the fixed project
#: secret `FIXED_A`, as the jitter-seed file asserts as a literal (it was
#: -286 before the offset was keyed in 0.9.7). One offset, in whichever
#: pass a value is first shifted (#517).
OFFSET = -364

MODES = ["threads", "processes"]


@pytest.fixture
def mode(request, monkeypatch):
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)
    if request.param == "threads":
        monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
        monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    else:
        monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")
        monkeypatch.delenv("ISOCENTER_FORCE_THREADS", raising=False)
    return request.param


@pytest.fixture(autouse=True)
def _threads_by_default(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _session(tmp_path, *, study_date=date(2023, 1, 1), instance_date=None,
             tags=None, name="m.db"):
    session = DicomSession(str(tmp_path / name))
    # The store is given a fixed project secret, so OFFSET is a literal
    # a reader can check (0.9.7): a generated secret would make it random.
    load_fixed_secret(session, tmp_path, FIXED_A)
    patient = Patient(PID, "Orig^Name")
    study = Study(STUDY_UID, study_date)
    series = Series(f"{STUDY_UID}.1", "OT", 1)
    instance = Instance(f"{STUDY_UID}.1.0", SC_SOP_CLASS, 1)
    if instance_date is not None:
        instance.set_attr(STUDY_DATE, instance_date)
    series.instances.append(instance)
    study.series.append(series)
    patient.studies.append(study)
    session.store.patients.append(patient)
    session.configuration.phi_tags = dict(tags or {})
    return session


def _study(session):
    return session.store.patients[0].studies[0]


def _instance(session):
    return _study(session).series[0].instances[0]


def _study_findings(report):
    """The study's date findings. Not its UID's: a reopen with no
    configuration scans under the floor, which replaces the Study Instance
    UID the pass under `phi_tags` alone left (#544)."""
    return [f for f in report.findings
            if f.entity_type == "Study" and f.tag == STUDY_DATE]


def test_a_hand_replaced_study_date_is_raised_and_shifted(tmp_path):
    """Case E, fixed. Red before: `raised=0`, `date_shifted=True`, and
    the fresh original was exported."""
    session = _session(tmp_path)
    with session:
        session.anonymize(session.audit())
        study = _study(session)
        first = study.study_date
        assert study.date_shifted
        assert (first - date(2023, 1, 1)).days == OFFSET

        study.study_date = date(2024, 7, 4)
        report = session.audit()
        assert len(_study_findings(report)) == 1
        session.anonymize(report)
        assert (study.study_date - date(2024, 7, 4)).days == OFFSET
        assert study.study_date != date(2024, 7, 4)


def test_an_untouched_shifted_study_still_raises_nothing(tmp_path):
    """The other direction. A record that never vouched would re-shift
    every study on every pass -- #513's shape, one level up."""
    session = _session(tmp_path)
    with session:
        session.anonymize(session.audit())
        study = _study(session)
        settled = study.study_date
        for _ in range(3):
            report = session.audit()
            assert _study_findings(report) == []
            session.anonymize(report)
        assert study.study_date == settled


@pytest.mark.parametrize("mode", MODES, indirect=True)
def test_a_worker_sees_the_studys_record(tmp_path, mode):
    """`_scan_study` runs inside `scan_patient`, which the worker calls
    on the lightweight clone. Without the record on the clone every
    worker sees `date_shifted` with no record -- a study that looks
    pre-0.9.6 -- and raises nothing, which is the whole fix invisible on
    both parallel paths at once."""
    session = _session(tmp_path)
    with session:
        session.anonymize(session.audit())
        study = _study(session)
        study.study_date = date(2024, 7, 4)
        assert len(_study_findings(session.audit())) == 1, mode


def test_the_record_is_compared_as_a_da_string(tmp_path):
    """Stored through `format_study_date`, so the record and the graph
    are held in one representation. Keep the raw object instead and a
    `date` hydrated from the store stops matching a record written as
    something else."""
    study = Study(STUDY_UID, date(2023, 1, 1))
    study.record_date_shift(date(2022, 3, 21))
    assert study._shifted_study_date == "20220321"
    assert study.date_shift_vouches_for(date(2022, 3, 21))
    # The DA string spelling of the same day, which `Study.__setattr__`
    # normalises to that `date`, must vouch too.
    assert study.date_shift_vouches_for("20220321")
    assert not study.date_shift_vouches_for(date(2024, 7, 4))
    # And no record vouches for nothing, whatever the flag says.
    fresh = Study(STUDY_UID, date(2023, 1, 1))
    fresh.date_shifted = True
    assert not fresh.date_shift_vouches_for(date(2023, 1, 1))


def test_the_owners_replacement_skip_asks_the_record_not_the_flag(tmp_path):
    """#518 through #496's door. An instance's top-level copy of the
    study date is skipped only when the owner's value is one this
    pipeline produced. Red before: the copy of a hand-replaced date
    skipped as "the owner's replacement", so the fresh original stayed
    on the instance too."""
    session = _session(tmp_path, instance_date="20230101",
                       tags={STUDY_DATE: {"name": "Study Date",
                                          "action": "SHIFT"}})
    with session:
        session.anonymize(session.audit())
        study, instance = _study(session), _instance(session)
        assert instance.attributes[STUDY_DATE] == format_study_date(
            study.study_date)

        # A hand-replaced study date, copied onto the instance the way
        # the owner's own write would have.
        study.study_date = date(2024, 7, 4)
        instance.set_attr(STUDY_DATE, "20240704")
        assert not _study_date_is_this_pipelines(study)

        inspector = PhiInspector(config_tags={
            STUDY_DATE: {"name": "Study Date", "action": "SHIFT"}})
        raised = inspector._scan_instance(instance, PID, study=study)
        assert [f.tag for f in raised] == [STUDY_DATE], raised

        report = session.audit()
        session.anonymize(report)
        assert instance.attributes[STUDY_DATE] != "20240704"
        assert instance.attributes[STUDY_DATE] == format_study_date(
            study.study_date)


def test_the_owners_replacement_skip_still_skips_a_value_it_produced(tmp_path):
    """#496's own case, unbroken: an instance's copy of the study's
    *shifted* date is still not raised, so a re-audit does not put a
    second value on it."""
    session = _session(tmp_path, instance_date="20230101",
                       tags={STUDY_DATE: {"name": "Study Date",
                                          "action": "SHIFT"}})
    with session:
        session.anonymize(session.audit())
        study, instance = _study(session), _instance(session)
        assert _study_date_is_this_pipelines(study)
        report = session.audit()
        assert [f for f in report.findings if f.tag == STUDY_DATE] == []
        session.anonymize(report)
        assert instance.attributes[STUDY_DATE] == format_study_date(
            study.study_date)


def test_the_record_survives_a_save_and_a_reload(tmp_path):
    """`studies.shifted_study_date`, written by the upsert and read by
    both hydration loops. Without it a reloaded study reads
    `True`/NULL -- the pre-0.9.6 shape -- and a fresh original written
    into `study_date` is hidden again."""
    session = _session(tmp_path)
    with session:
        session.anonymize(session.audit())
        shifted = _study(session).study_date
        session.save(sync=True)

    with sqlite3.connect(str(tmp_path / "m.db")) as conn:
        stored = [row[0] for row in conn.execute(
            "SELECT shifted_study_date FROM studies")]
    assert stored == [format_study_date(shifted)], stored

    reopened = DicomSession(str(tmp_path / "m.db"))
    with reopened:
        study = _study(reopened)
        assert study.date_shifted
        assert study._shifted_study_date == format_study_date(shifted)
        assert study.study_date == shifted
        assert _study_findings(reopened.audit()) == []

        study.study_date = date(2024, 7, 4)
        assert len(_study_findings(reopened.audit())) == 1


def test_load_patient_restores_the_record_too(tmp_path):
    """The second hydration loop, which is a separate copy of the same
    two lines and has drifted from `load_all` before."""
    session = _session(tmp_path)
    with session:
        session.anonymize(session.audit())
        shifted = _study(session).study_date
        stored_id = session.store.patients[0].patient_id
        session.save(sync=True)

    reopened = DicomSession(str(tmp_path / "m.db"))
    with reopened:
        loaded = reopened.store_backend.load_patient(stored_id)
        assert loaded is not None
        assert loaded.studies[0]._shifted_study_date == format_study_date(
            shifted)


def test_a_record_with_no_flag_is_read_as_the_record(tmp_path):
    """The fourth row of the truth table. The arm sets both, so a row
    with a record and no flag is hand-edited or partially written; the
    record is the more specific claim, so it wins in both directions."""
    study = Study(STUDY_UID, date(2022, 3, 21))
    study.record_date_shift(date(2022, 3, 21))
    assert not study.date_shifted
    assert _study_date_is_this_pipelines(study)
    study.study_date = date(2024, 7, 4)
    assert not _study_date_is_this_pipelines(study)


def test_the_study_flag_is_still_set_and_still_public(tmp_path):
    """`Study.date_shifted` is deliberately kept where
    `Instance.date_shifted` was cut: it answers the entity-level
    question honestly and `exporters/wfdb.py` reads it for the header's
    `# de-identified start date:` comment
    (`tests/test_date_shifted_roundtrip.py` is that pin)."""
    session = _session(tmp_path)
    with session:
        session.anonymize(session.audit())
        study = _study(session)
        assert study.date_shifted is True
        assert study._shifted_study_date == format_study_date(study.study_date)

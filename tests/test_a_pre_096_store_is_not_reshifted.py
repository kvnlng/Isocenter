"""An instance written before 0.9.6 keeps the old rule, and says so (#510).

Shifted-ness is recorded per value since 0.9.6, and a row written before
those records existed carries none. Reading "no record" as "not shifted"
would make the first `audit()` after upgrading raise every already-shifted
date in the store and `anonymize()` shift each one a second time, with a
`REMEDIATION_SHIFT_DATE` row that looks legitimate -- #513 applied to a
whole archive, caused by the fix for it. So the owner's ruling
(2026-09-12) is that nothing in an existing store changes under the user:
such an instance keeps the pre-0.9.6 entity-level rule for the values it
already holds, permanently, and the load says so once.

**What the store carries, and why it needs a column at all.** `NULL` in
`instances.shift_provenance` means "this row predates per-value records";
`'recorded'` means it was written by 0.9.6 or later, so its records are
the whole truth. That is the one migration in `persistence.py` where the
absent column is the *unsafe* reading, which is why it is a column rather
than a NULL-reads-False flag like `studies.date_shifted` (#182).

NULL alone is not enough to mark an instance legacy: it says the row is
old, not that anything was ever shifted. The only persisted evidence
anywhere that a shift ran is the **study's** `date_shifted`, because
`Instance.date_shifted` never had a column (and is gone). An old-store
instance under an unshifted study therefore has nothing to protect --
today's code already re-shifts such a date -- and marking it legacy would
hide a rule added in a later pass once its study is shifted under 0.9.6,
which is #510 persisting for an instance with no real legacy.

**The study half (#518) is the same ruling one level up**, and needs no
provenance column of its own: `studies.date_shifted` with no
`shifted_study_date` already means "shifted before 0.9.6, value
unknowable", and such a study keeps the pre-0.9.6 rule too. The instance
half needed a column precisely because `Instance.date_shifted` was never
persisted, so an instance row carried no witness at all.

**The notice.** One `WARNING` audit row and one log line per *load*, not
per instance and not one per level, counting the affected instances and
studies and naming the guarantee that does not apply. A `WARNING` row grades the session
`REVIEW_REQUIRED` (#479), which is intended: a session that cannot answer
the question for part of its graph should say so, in the compliance
report and not only on a console.

**How the fixture is built.** By writing a 0.9.6 store and then taking
the column away -- `ALTER TABLE ... DROP COLUMN`, the shape
`tests/test_date_shifted_roundtrip.py` already uses for `date_shifted` --
plus stripping `__shifted__` out of `attributes_json`. Hand-writing the
whole pre-0.9.6 schema would be a second copy of it that could drift from
the real one; this way the guarded ALTER is exercised too, because
reopening is what adds the column back as NULL.

**Why this file imports what it does.** It reaches hydration and the
migration through `isocenter.persistence` and the rule through
`isocenter.privacy`, driving both from `isocenter.session`; see
`test_mutation_probe_targets.py`.
"""
import json
import sqlite3
from datetime import date

import pytest

from isocenter.entities import Instance, Patient, Series, Study
from isocenter.session import DicomSession

SC_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.7"
ACQ_DATE = "0008,0022"
CONTENT_DATE = "0008,0023"
SHIFT_ACQ = {ACQ_DATE: {"name": "AcquisitionDate", "action": "SHIFT"}}
SHIFT_BOTH = {**SHIFT_ACQ,
              CONTENT_DATE: {"name": "ContentDate", "action": "SHIFT"}}


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _write_store(tmp_path, *, study_date, count=1, tags=SHIFT_ACQ,
                 anonymize=True, name="legacy.db"):
    """A 0.9.6 store: `count` instances, each with a date under `tags`."""
    db_path = str(tmp_path / name)
    session = DicomSession(db_path)
    patient = Patient("P1", "Orig^Name")
    study = Study("1.2.826.0.1.510L", study_date)
    series = Series("1.2.826.0.1.510L.1", "OT", 1)
    for index in range(count):
        instance = Instance(f"1.2.826.0.1.510L.1.{index}", SC_SOP_CLASS,
                            index + 1)
        instance.set_attr(ACQ_DATE, "20230601")
        series.instances.append(instance)
    study.series.append(series)
    patient.studies.append(study)
    session.store.patients.append(patient)
    session.configuration.phi_tags = dict(tags)
    with session:
        if anonymize:
            session.anonymize(session.audit())
        shifted = [i.attributes[ACQ_DATE] for i in series.instances]
        session.save(sync=True)
    return db_path, shifted


def _age_the_store(db_path):
    """Take the store back to the pre-0.9.6 shape: no provenance column,
    and no `__shifted__` key in any instance's JSON."""
    with sqlite3.connect(db_path) as conn:
        conn.execute("ALTER TABLE instances DROP COLUMN shift_provenance")
        # The study half of the same migration (#518): a pre-0.9.6 store
        # has neither column, and a study with `date_shifted` set and no
        # record is one whose shifted value is unknowable.
        conn.execute("ALTER TABLE studies DROP COLUMN shifted_study_date")
        rows = conn.execute(
            "SELECT sop_instance_uid, attributes_json FROM instances").fetchall()
        for uid, blob in rows:
            data = json.loads(blob)
            data.pop("__shifted__", None)
            conn.execute(
                "UPDATE instances SET attributes_json = ? "
                "WHERE sop_instance_uid = ?",
                (json.dumps(data), uid))


def _warnings(db_path):
    with sqlite3.connect(db_path) as conn:
        return [row[0] for row in conn.execute(
            "SELECT details FROM audit_log WHERE action_type='WARNING'")]


def _shift_rows(db_path):
    with sqlite3.connect(db_path) as conn:
        return [row[0] for row in conn.execute(
            "SELECT details FROM audit_log "
            "WHERE action_type='REMEDIATION_SHIFT_DATE'")]


def _declines(db_path):
    with sqlite3.connect(db_path) as conn:
        return [row[0] for row in conn.execute(
            "SELECT details FROM audit_log "
            "WHERE action_type='REMEDIATION_DECLINED'")]


def _instances(session):
    return session.store.patients[0].studies[0].series[0].instances


def test_a_pre_096_instance_is_not_shifted_a_second_time(tmp_path):
    """The migration hazard, from the wrong side. Read "no record" as
    "not shifted" and this store's already-shifted date is raised and
    shifted again."""
    db_path, shifted = _write_store(tmp_path, study_date=date(2023, 1, 1))
    _age_the_store(db_path)
    before = len(_shift_rows(db_path))

    reopened = DicomSession(db_path)
    with reopened:
        reopened.configuration.phi_tags = dict(SHIFT_ACQ)
        instance = _instances(reopened)[0]
        assert instance._legacy_shift_provenance
        assert instance.attributes[ACQ_DATE] == shifted[0]
        report = reopened.audit()
        assert [f for f in report.findings if f.tag == ACQ_DATE] == []
        reopened.anonymize(report)
        assert instance.attributes[ACQ_DATE] == shifted[0]

    assert len(_shift_rows(db_path)) == before


def test_an_old_instance_under_an_unshifted_study_is_not_legacy(tmp_path):
    """NULL provenance says the row is old, not that anything was
    shifted. An instance whose study was never shifted has nothing to
    protect, and marking it legacy would hide #510 for ever on a graph
    with no real legacy -- so its date is still raised."""
    db_path, _ = _write_store(tmp_path, study_date=None, anonymize=False)
    _age_the_store(db_path)

    reopened = DicomSession(db_path)
    with reopened:
        reopened.configuration.phi_tags = dict(SHIFT_ACQ)
        instance = _instances(reopened)[0]
        assert not instance._legacy_shift_provenance
        assert not reopened.store.patients[0].studies[0].date_shifted
        report = reopened.audit()
        assert len([f for f in report.findings if f.tag == ACQ_DATE]) == 1
        reopened.anonymize(report)
        assert instance.attributes[ACQ_DATE] != "20230601"

    assert len(_warnings(db_path)) == 0, "nothing here is legacy; say nothing"


def test_a_legacy_instance_stays_legacy_across_a_re_save(tmp_path):
    """The subtle one. A legacy instance is written back by the next
    save -- `audit()` alone dirties it, by stamping a status -- and the
    upsert must write its `NULL` through rather than stamping
    `'recorded'` over it. Stamp it, and one re-save costs the instance
    the protection its unrecorded dates depend on: the load after that
    raises them, and `anonymize()` shifts each a second time."""
    db_path, shifted = _write_store(tmp_path, study_date=date(2023, 1, 1))
    _age_the_store(db_path)

    first = DicomSession(db_path)
    with first:
        first.configuration.phi_tags = dict(SHIFT_ACQ)
        first.audit()
        first.save(sync=True)

    with sqlite3.connect(db_path) as conn:
        stored = [row[0] for row in conn.execute(
            "SELECT shift_provenance FROM instances")]
    assert stored == [None], stored

    second = DicomSession(db_path)
    with second:
        second.configuration.phi_tags = dict(SHIFT_ACQ)
        instance = _instances(second)[0]
        assert instance._legacy_shift_provenance
        report = second.audit()
        assert [f for f in report.findings if f.tag == ACQ_DATE] == []
        second.anonymize(report)
        assert instance.attributes[ACQ_DATE] == shifted[0]


def test_a_store_this_version_wrote_is_not_legacy(tmp_path):
    """The other direction, so the flag is not simply always True. A
    0.9.6 store carries `'recorded'` and its records are the whole
    truth: a value replaced after its shift is raised again, which is
    exactly what a legacy instance cannot do."""
    db_path, shifted = _write_store(tmp_path, study_date=date(2023, 1, 1))
    with sqlite3.connect(db_path) as conn:
        stored = [row[0] for row in conn.execute(
            "SELECT shift_provenance FROM instances")]
    assert stored == ["recorded"], stored

    reopened = DicomSession(db_path)
    with reopened:
        reopened.configuration.phi_tags = dict(SHIFT_ACQ)
        instance = _instances(reopened)[0]
        assert not instance._legacy_shift_provenance
        assert [f for f in reopened.audit().findings if f.tag == ACQ_DATE] == []
        instance.set_attr(ACQ_DATE, "20240704")
        assert len([f for f in reopened.audit().findings
                    if f.tag == ACQ_DATE]) == 1
    assert shifted[0] != "20230601"
    assert _warnings(db_path) == []


def test_a_declined_date_on_a_legacy_instance_is_still_raised(tmp_path):
    """#498 must not regress on a legacy store, which is why the legacy
    branch asks `remediation._date_shift_declines` rather than simply
    skipping.

    "The pre-0.9.6 rule" means #498's version of it, not the older one: a
    value the arm cannot parse is raised again so its decline recurs and
    #491's pass-end demotion keeps the instance IDENTIFIED, while a value
    the shift could apply to is skipped because it has already moved. Both
    directions are asserted on one instance, because a legacy branch that
    skipped everything would be green on the other test in this file.
    """
    db_path = str(tmp_path / "legacy_decline.db")
    session = DicomSession(db_path)
    patient = Patient("P1", "Orig^Name")
    study = Study("1.2.826.0.1.510D", date(2023, 1, 1))
    series = Series("1.2.826.0.1.510D.1", "OT", 1)
    instance = Instance("1.2.826.0.1.510D.1.0", SC_SOP_CLASS, 1)
    instance.set_attr(ACQ_DATE, "20230601")
    instance.set_attr(CONTENT_DATE, "notadate")
    series.instances.append(instance)
    study.series.append(series)
    patient.studies.append(study)
    session.store.patients.append(patient)
    session.configuration.phi_tags = dict(SHIFT_BOTH)
    with session:
        session.anonymize(session.audit())
        shifted = instance.attributes[ACQ_DATE]
        assert shifted != "20230601"
        assert instance.attributes[CONTENT_DATE] == "notadate"
        session.save(sync=True)
    _age_the_store(db_path)
    declines_before = _declines(db_path)

    reopened = DicomSession(db_path)
    with reopened:
        reopened.configuration.phi_tags = dict(SHIFT_BOTH)
        loaded = _instances(reopened)[0]
        assert loaded._legacy_shift_provenance
        report = reopened.audit()
        raised = sorted(f.tag for f in report.findings
                        if f.tag in (ACQ_DATE, CONTENT_DATE))
        assert raised == [CONTENT_DATE], raised
        reopened.anonymize(report)
        assert loaded.attributes[ACQ_DATE] == shifted
        assert loaded.attributes[CONTENT_DATE] == "notadate"

    assert len(_declines(db_path)) == len(declines_before) + 1


@pytest.mark.parametrize("value", ["2023060510", "202306051048"])
def test_an_hour_or_minute_precision_datetime_on_a_legacy_instance_is_raised(tmp_path, value):
    """#559's parser must not widen, and this is the store it would harm
    (review of #574). The pre-0.9.6 parser declined `2023060510` (and
    shifted a `...1048` only by misreading it), so a legacy instance
    holding one after its study was shifted still holds it unshifted.
    The legacy branch reads "would shift" as "already shifted"; a parser
    that shifts hour- and minute-precision DateTimes therefore grades this
    instance CLEARED with the value retained. Both precisions decline, so
    the value is raised, the decline recurs, and the instance stays
    IDENTIFIED."""
    dt_tag = "0008,002a"
    tags = {dt_tag: {"name": "AcquisitionDateTime", "action": "JITTER"}}
    db_path = str(tmp_path / "legacy_dt.db")
    session = DicomSession(db_path)
    patient = Patient("P1", "Orig^Name")
    study = Study("1.2.826.0.1.510T", "20230601")
    series = Series("1.2.826.0.1.510T.1", "OT", 1)
    instance = Instance("1.2.826.0.1.510T.1.0", SC_SOP_CLASS, 1)
    series.instances.append(instance)
    study.series.append(series)
    patient.studies.append(study)
    session.store.patients.append(patient)
    session.configuration.phi_tags = dict(tags)
    with session:
        session.anonymize(session.audit())
        assert study.date_shifted
        # What a pre-0.9.6 pass left: the value its parser declined.
        instance.set_attr(dt_tag, value)
        session.save(sync=True)
    _age_the_store(db_path)

    reopened = DicomSession(db_path)
    with reopened:
        reopened.configuration.phi_tags = dict(tags)
        loaded = _instances(reopened)[0]
        assert loaded._legacy_shift_provenance
        report = reopened.audit()
        assert [f.tag for f in report.findings if f.tag == dt_tag] == [dt_tag]
        reopened.anonymize(report)
        assert loaded.attributes[dt_tag] == value
        assert loaded.phi_status.name == "IDENTIFIED", loaded.phi_status


def test_a_pre_096_study_date_is_not_raised_either(tmp_path):
    """The study half of the same ruling (#518). `date_shifted` set with
    no record means "shifted before 0.9.6, value unknowable", so the
    study keeps the pre-0.9.6 rule: its date is not raised, even after a
    hand edit, because nothing here can tell a fresh original from the
    shift's own output.

    Stated as its own case because the truth table has four rows and
    this is the one that has to read the two columns *together*: the
    same NULL under `date_shifted = 0` means "never shifted" and must
    raise.
    """
    db_path, _ = _write_store(tmp_path, study_date=date(2023, 1, 1))
    _age_the_store(db_path)

    reopened = DicomSession(db_path)
    with reopened:
        study = reopened.store.patients[0].studies[0]
        assert study.date_shifted and study._shifted_study_date is None
        settled = study.study_date
        # The date only: this store's pass ran under `phi_tags` alone, and
        # the reopen scans under the floor, which replaces the Study
        # Instance UID (#544).
        assert [f for f in reopened.audit().findings
                if f.entity_type == "Study" and f.field_name == "study_date"] == []
        study.study_date = date(2024, 7, 4)
        assert [f for f in reopened.audit().findings
                if f.entity_type == "Study" and f.field_name == "study_date"] == []
        reopened.anonymize(reopened.audit())
        assert study.study_date == date(2024, 7, 4)
    assert settled != date(2023, 1, 1)


def test_the_notice_counts_studies_as_well_as_instances(tmp_path):
    """One notice for one limitation at two levels, not two rows. An
    operator reading two WARNING rows about one store would reasonably
    think there were two problems."""
    db_path, _ = _write_store(tmp_path, study_date=date(2023, 1, 1), count=2)
    _age_the_store(db_path)

    reopened = DicomSession(db_path)
    with reopened:
        pass

    rows = _warnings(db_path)
    assert len(rows) == 1, rows
    assert "2 instances and 1 study" in rows[0], rows[0]


def test_a_record_still_vouches_on_a_legacy_instance(tmp_path):
    """The legacy fallback covers only the values nobody can name. A
    legacy instance still records what a shift writes, so a value
    shifted under 0.9.6 is answered exactly rather than by the
    fallback -- and the fallback shrinks as the store is worked on
    instead of being a permanent blanket over the instance."""
    db_path, _ = _write_store(tmp_path, study_date=date(2023, 1, 1))
    _age_the_store(db_path)

    reopened = DicomSession(db_path)
    with reopened:
        instance = _instances(reopened)[0]
        assert instance._legacy_shift_provenance
        instance.record_date_shift(CONTENT_DATE, "20220101")
        instance.set_attr(CONTENT_DATE, "20220101")
        assert instance.date_shift_vouches_for(CONTENT_DATE, "20220101")
        instance.set_attr(CONTENT_DATE, "20240704")
        assert not instance.date_shift_vouches_for(CONTENT_DATE, "20240704")


def test_the_load_says_so_once_naming_the_guarantee_that_does_not_apply(tmp_path):
    """One `WARNING` row and one log line per load, not per instance:
    three instances, one row, counting them. The wording has to name
    which guarantee is missing (#510's), the failure direction (a real
    date may survive; a double shift never happens) and the remedy."""
    db_path, _ = _write_store(tmp_path, study_date=date(2023, 1, 1), count=3)
    _age_the_store(db_path)

    reopened = DicomSession(db_path)
    with reopened:
        assert all(i._legacy_shift_provenance for i in _instances(reopened))

    rows = _warnings(db_path)
    assert len(rows) == 1, rows
    (row,) = rows
    assert "3 instances" in row, row
    assert "0.9.6" in row and "#510" in row
    assert "never shifted twice" in row
    assert "Re-ingesting" in row


def test_the_notice_grades_the_session_review_required(tmp_path):
    """It is a `WARNING` row so that the limitation reaches the
    compliance report, and a section-4 row grades the run
    `REVIEW_REQUIRED` (#479). A session that cannot answer the question
    for part of its graph should not read PASS."""
    db_path, _ = _write_store(tmp_path, study_date=date(2023, 1, 1))
    _age_the_store(db_path)

    reopened = DicomSession(db_path)
    with reopened:
        reopened.configuration.phi_tags = dict(SHIFT_ACQ)
        reopened.anonymize(reopened.audit())
        out = tmp_path / "report.md"
        reopened.generate_report(str(out))
        text = out.read_text(encoding="utf-8")

    assert "REVIEW_REQUIRED" in text, text[:2000]
    assert "per-value date records" in text, (
        "the report must carry the limitation, not only the grade")


def test_the_load_says_nothing_for_a_store_with_nothing_to_say(tmp_path):
    """No cry-wolf: a 0.9.6 store writes no row and logs no line."""
    db_path, _ = _write_store(tmp_path, study_date=date(2023, 1, 1))
    reopened = DicomSession(db_path)
    with reopened:
        pass
    assert _warnings(db_path) == []


def test_load_patient_marks_legacy_the_same_way(tmp_path):
    """The second hydration loop, which is a separate copy of this logic
    and has drifted from `load_all` before. It has the study in scope
    rather than through a map, so both spellings need their own case."""
    db_path, shifted = _write_store(tmp_path, study_date=date(2023, 1, 1))
    _age_the_store(db_path)

    reopened = DicomSession(db_path)
    with reopened:
        # By the id the store holds: `anonymize()` replaced `P1`.
        stored_id = reopened.store.patients[0].patient_id
        loaded = reopened.store_backend.load_patient(stored_id)
        assert loaded is not None
        instance = loaded.studies[0].series[0].instances[0]
        assert instance._legacy_shift_provenance
        assert instance.attributes[ACQ_DATE] == shifted[0]

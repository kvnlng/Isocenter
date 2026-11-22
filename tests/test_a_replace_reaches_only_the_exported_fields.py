"""A hand-built `REPLACE_TAG` on a Patient, Study or Series writes only
the fields the export writes from it (#987; owner rulings Q1a-Q1c,
2026-11-21).

The Python-attribute arm was generic over every name the entity carries.
A finding naming `Patient.studies`, `Study.series` or `Series.instances`
set the container to a string, returned 1, wrote `REMEDIATION_REPLACE`
and stamped REMEDIATED; every later `save()`, `export()` and
`generate_report()` raised `AttributeError`. A private field
(`_jitter_scheme`) was written and saved. `Series.modality` reached the
store and the DataFrame but no exported file; `Study.date_shifted` is
bookkeeping no scan raises.

Now the arm takes `ENTITY_FIELD_TAGS` as its allowlist, as #679 made the
REMOVE arm do: Patient's Name and ID, Study Date, and the Study and
Series Instance UIDs. Any other name declines with one
`REMEDIATION_DECLINED` row naming the attribute and the type, never a
value, and nothing is written.

**Why this file imports what it does.** The pipeline through
`isocenter.session`, hand-built findings through `isocenter.privacy`,
the service and its table through `isocenter.remediation`: those three
rows of the probe's `TARGETS`.
"""
import sqlite3

import pytest

from isocenter.privacy import PhiFinding, PhiRemediation
from isocenter.remediation import RemediationService
from isocenter.session import DicomSession
from support.ct_small_files import write_ct

EXPORTED = "is not a field the export writes from the"


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)


def _rows(session):
    session.store_backend.flush_audit_queue()
    with sqlite3.connect(session.store_backend.db_path) as conn:
        return conn.execute(
            "SELECT action_type, details FROM audit_log ORDER BY rowid").fetchall()


def _finding(entity, entity_type, uid, attr, new, original):
    return PhiFinding(
        entity_uid=uid, entity_type=entity_type, field_name=attr, value=original,
        reason="hand-built", entity=entity, tag=None,
        remediation_proposal=PhiRemediation(
            action_type="REPLACE_TAG", target_attr=attr,
            new_value=new, original_value=original))


def _patient(p):
    return p, "Patient", p.patient_id


def _study(p):
    st = p.studies[0]
    return st, "Study", st.study_instance_uid


def _series(p):
    se = p.studies[0].series[0]
    return se, "Series", se.series_instance_uid


CASES = {
    "Patient.studies": (_patient, "studies", "ANONYMIZED"),
    "Study.series": (_study, "series", "ANONYMIZED"),
    "Series.instances": (_series, "instances", "ANONYMIZED"),
    "Patient._jitter_scheme": (_patient, "_jitter_scheme", "ANONYMIZED"),
    "Series.modality": (_series, "modality", "OT"),
    "Study.date_shifted": (_study, "date_shifted", True),
    "Study.study_time": (_study, "study_time", "120000"),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_a_replace_on_a_field_the_export_does_not_write_declines(tmp_path, case):
    """One decline naming the attribute and the type, the field as it was,
    nothing applied, and the session still saves, exports and reports.
    Red before on every case: written, applied 1, and on the three
    containers every later step raised `AttributeError`."""
    write_ct(tmp_path / "src" / "a.dcm", "PID-987", 987, name="Alpha^One")
    pick, attr, new = CASES[case]
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "src"))
        entity, entity_type, uid = pick(session.store.patients[0])
        held = getattr(entity, attr)
        before = len(_rows(session))

        applied = session.anonymize([_finding(entity, entity_type, uid, attr, new, held)])

        assert applied == 0
        assert getattr(entity, attr) is held
        rows = _rows(session)[before:]
        assert [a for a, _ in rows] == ["REMEDIATION_DECLINED"], rows
        assert f"{attr} {EXPORTED} {entity_type}" in rows[0][1], rows
        assert str(new) not in rows[0][1].split(": ", 1)[1], rows
        session.save(sync=True)
        session.export(str(tmp_path / "out"), use_compression=False)
        session.generate_report(str(tmp_path / "r.md"))


def test_every_field_the_replace_arm_may_write_is_an_exported_field():
    """The allowlist is `ENTITY_FIELD_TAGS`, read from the class; its REMOVE
    twin is `test_every_field_the_remove_arm_may_clear_is_a_tracked_field`.
    Five fields, each the export writes from its owner."""
    service = RemediationService()
    for field in ("studies", "series", "instances", "modality", "date_shifted",
                  "study_time", "_jitter_scheme"):
        assert field not in RemediationService.ENTITY_FIELD_TAGS
    assert set(RemediationService.ENTITY_FIELD_TAGS) == {
        "patient_name", "patient_id", "study_date",
        "study_instance_uid", "series_instance_uid"}

    class Holder:  # pylint: disable=too-few-public-methods
        """An entity carrying every name, so only the allowlist decides."""
        patient_name = patient_id = study_date = "x"
        study_instance_uid = series_instance_uid = modality = "x"

    for field in RemediationService.ENTITY_FIELD_TAGS:
        reason = service._replace_attr_refused(  # pylint: disable=protected-access
            Holder(), PhiRemediation(action_type="REPLACE_TAG", target_attr=field,
                                     new_value="y"))
        assert reason is None or EXPORTED not in reason, (field, reason)
    reason = service._replace_attr_refused(  # pylint: disable=protected-access
        Holder(), PhiRemediation(action_type="REPLACE_TAG", target_attr="modality",
                                 new_value="y"))
    assert EXPORTED in reason

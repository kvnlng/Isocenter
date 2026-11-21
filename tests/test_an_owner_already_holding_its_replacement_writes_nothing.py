"""A Patient, Study or Series that already holds what a proposal writes,
with the record to vouch for it, is satisfied (#990).

Before, a whole report handed to `anonymize()` a second time wrote every
owner-level remediation again: the Patient ID and Name, the Study and
Series Instance UIDs and the Study's date shift, each onto the value
already there, each counted and logged. Under the floor the second call
returned 4 and report section 2 counted one shift twice.

"Already there" on an owner is read with the record, never on equality
alone: the Patient ID and the owned UIDs must be this store's mint (the
refusals ahead of the test check that), and every instance copy the
owner's write would reach must already hold the value with the
instance's remediation record. An owner with no copies at all is read on
equality. These tests pin the second call's quiet, and the three guards
that keep it from reading a value as done when it is not.

**Why this file imports what it does.** The pipeline through
`isocenter.session`, hand-built findings through `isocenter.privacy`:
the `session`, `remediation` and `privacy` rows of the probe's
`TARGETS`.
"""
import sqlite3

import pydicom
import pytest

from isocenter.privacy import PhiFinding, PhiRemediation
from isocenter.session import DicomSession
from support.ct_small_files import study_uid, write_ct
from support.project_secret import FIXED_A, load_fixed_secret

SHARED = 990

POLICIES = {
    "floor-name-remove": "phi_tags:\n  '0010,0010': {action: REMOVE}\n",
    "basic": "privacy_profile: basic\nphi_tags: {}\n",
    "none-name-remove": "privacy_profile: none\nphi_tags:\n  '0010,0010': {action: REMOVE}\n",
}
OWNER_FIELDS = ("patient_id", "patient_name", "study_instance_uid",
                "series_instance_uid", "study_date")


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


def _write(path, suffix):
    write_ct(path, "PA", SHARED, name="Alpha^Ann")
    ds = pydicom.dcmread(str(path))
    ds.SeriesInstanceUID = f"{study_uid(SHARED)}.{suffix}"
    ds.SOPInstanceUID = f"{study_uid(SHARED)}.{suffix}.1"
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.save_as(str(path))


def _session(tmp_path, policy):
    _write(tmp_path / "a" / "a.dcm", 1)
    cfg = tmp_path / "c.yaml"
    cfg.write_text(POLICIES[policy])
    session = DicomSession(str(tmp_path / "s.db"))
    load_fixed_secret(session, tmp_path, FIXED_A)
    session.load_config(str(cfg))
    session.ingest(str(tmp_path / "a"))
    return session


@pytest.mark.parametrize("policy", sorted(POLICIES))
def test_the_second_pass_returns_zero_and_writes_no_owner_row(tmp_path, policy):
    with _session(tmp_path, policy) as session:
        report = session.audit()
        assert session.anonymize(report) > 0
        session.save(sync=True)
        before = len(_rows(session))

        assert session.anonymize(report) == 0

        assert _rows(session)[before:] == []


def test_the_second_pass_leaves_section_two_counting_each_remediation_once(tmp_path):
    with _session(tmp_path, "floor-name-remove") as session:
        report = session.audit()
        session.anonymize(report)
        session.anonymize(report)
        path = tmp_path / "report.md"
        session.generate_report(str(path))
    lines = [line for line in path.read_text(encoding="utf-8").splitlines()
             if "REMEDIATION_SHIFT_DATE" in line]
    assert lines == ["| REMEDIATION_SHIFT_DATE | 1 |"], lines


def test_an_unrecorded_copy_under_an_owner_already_replaced_is_written_once(tmp_path):
    """A file of the study ingested after the first pass (#894's shape)
    carries a copy the owner's write never reached. The owner is not
    "already there" while that copy lacks the value and its record, so it
    writes once more, and the new copy is the owner's value with its
    record. Read on the owner alone, the new copy would be left as
    ingested."""
    _write(tmp_path / "b" / "b.dcm", 2)
    with _session(tmp_path, "floor-name-remove") as session:
        report = session.audit()
        session.anonymize(report)
        session.save(sync=True)
        study = session.store.patients[0].studies[0]
        first = {id(i) for se in study.series for i in se.instances}
        session.ingest(str(tmp_path / "b"))
        new = [i for p in session.store.patients for st in p.studies
               for se in st.series for i in se.instances if id(i) not in first]
        assert len(new) == 1
        assert not new[0].remediation_vouches_for("0020,000d",
                                                  new[0].attributes.get("0020,000d"))
        before = len(_rows(session))

        session.anonymize(report)

        assert new[0].attributes["0020,000d"] == study.study_instance_uid
        assert new[0].remediation_vouches_for("0020,000d", study.study_instance_uid)
        rows = [d for a, d in _rows(session)[before:] if a == "REMEDIATION_REPLACE"]
        assert any(": study_instance_uid -> " in d for d in rows), rows


def test_a_foreign_pseudonym_equal_to_the_held_value_is_still_refused(tmp_path):
    """The refusals run before "already there": a `patient_id` REPLACE
    whose value is the one the patient holds, but whose original does not
    key to that patient, still declines with its row."""
    with _session(tmp_path, "floor-name-remove") as session:
        session.anonymize(session.audit())
        session.save(sync=True)
        patient = session.store.patients[0]
        held = patient.patient_id
        assert held.startswith("ANON_")
        before = len(_rows(session))
        finding = PhiFinding(
            entity_uid=held, entity_type="Patient", field_name="Patient ID",
            value="SOMEONE-ELSE", reason="hand-built", entity=patient, tag="0010,0020",
            remediation_proposal=PhiRemediation(
                action_type="REPLACE_TAG", target_attr="patient_id",
                new_value=held, original_value="SOMEONE-ELSE"))

        assert session.anonymize([finding]) == 0

        rows = _rows(session)[before:]
        assert [a for a, _ in rows] == ["REMEDIATION_DECLINED"], rows
        assert patient.patient_id == held


def test_a_study_holding_its_shifted_date_without_the_record_is_still_written(tmp_path):
    """The record, not equality: a Study set by hand to the date its
    shift writes, with no shift record, is shifted once with its row."""
    with _session(tmp_path, "floor-name-remove") as session:
        report = session.audit()
        session.anonymize(report)
        study = session.store.patients[0].studies[0]
        shifted = study.study_date
        study._shifted_study_date = None  # pylint: disable=protected-access
        assert not study.date_shift_vouches_for(shifted)
        before = len(_rows(session))

        session.anonymize(report)

        rows = [a for a, _ in _rows(session)[before:]]
        assert rows.count("REMEDIATION_SHIFT_DATE") == 1, rows
        assert study.study_date == shifted
        assert study.date_shift_vouches_for(shifted)

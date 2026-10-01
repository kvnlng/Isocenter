"""An entity edited after its scan and before `anonymize()` is not stamped
REMEDIATED by the pass (#752, owner ruling Q3 A, 2026-10-01).

Measured at 7579d4df on CT_small: `audit()`, then
`inst.set_attr("0008,0090", "Real^Referrer")` (the instance now reads
UNSCANNED, its status stale), then `anonymize(report)`. The pass's
success stamps recorded REMEDIATED whatever the status had been, so the
instance came out with a current REMEDIATED. The file carried
`Real^Referrer` beside `(0012,0062) YES`, the run graded PASS, and the
report said every instance had a scan at its current revision.

Now an entity whose status was stale when the pass began is still stale
when it ends, carrying the status the scan left behind
(`Session._keep_stale`). Grade condition 8 counts it, the markers are
withheld, and `audit()` is the way back. An entity never scanned is
unchanged: a pass over hand-built findings still stamps it REMEDIATED.
"""
import os
import sqlite3

import pydicom
import pytest

from isocenter import Session
from isocenter.entities import PhiStatus
from isocenter.session import _edited_since_its_status

from support.ct_small_files import write_ct

CONDITION_8 = ("edited after the last PHI scan: its content was changed after "
               "its PHI status was recorded, and no scan has read the change; "
               "`audit()` reads it ({levels})")


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _source(tmp_path, referrer=None):
    path = write_ct(tmp_path / "in" / "a.dcm", "PA", 752, name="Alpha^Ann")
    if referrer is not None:
        ds = pydicom.dcmread(path)
        ds.ReferringPhysicianName = referrer
        ds.save_as(path)


def _only(session):
    [patient] = session.store.patients
    [study] = patient.studies
    [series] = study.series
    [inst] = series.instances
    return patient, study, inst


def _export_and_report(session, tmp_path):
    session.export(str(tmp_path / "out"), use_compression=False,
                   show_progress=False)
    files = [os.path.join(root, f) for root, _, names in os.walk(tmp_path / "out")
             for f in names if f.endswith(".dcm")]
    assert len(files) == 1
    path = tmp_path / "report.md"
    session.generate_report(str(path))
    return pydicom.dcmread(files[0]), path.read_text(encoding="utf-8")


def _basis(text):
    return [line for line in text.splitlines() if "**Grade Basis:**" in line]


def _review_with_condition_8(text, levels):
    basis = _basis(text)
    assert len(basis) == 1 and "REVIEW_REQUIRED" in basis[0], basis
    assert CONDITION_8.format(levels=levels) in text, text


def test_an_instance_edited_after_its_scan_stays_stale(tmp_path):
    """CT_small's referrer is blank, so the scan raised nothing on it."""
    _source(tmp_path)
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        report = session.audit()
        assert [f for f in report.findings if f.tag == "0008,0090"] == []
        _, _, inst = _only(session)
        assert inst.phi_status is PhiStatus.IDENTIFIED
        inst.set_attr("0008,0090", "Real^Referrer")
        session.anonymize(report)
        assert inst.phi_status is PhiStatus.UNSCANNED
        assert _edited_since_its_status(inst)
        ds, text = _export_and_report(session, tmp_path)
    assert str(ds.ReferringPhysicianName) == "Real^Referrer"
    assert 0x00120062 not in ds
    assert 0x00120063 not in ds
    _review_with_condition_8(text, "patients 0, studies 0, instances 1")
    assert "1 entity edited after the last PHI scan" in text


def test_an_edited_tag_that_had_a_finding_is_still_not_stamped(tmp_path):
    """The pass's EMPTY overwrites the edit, so the value is no evidence
    here; the status, the markers and the grade are."""
    _source(tmp_path, referrer="Orig^Ref")
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        report = session.audit()
        assert [f.tag for f in report.findings if f.tag == "0008,0090"]
        _, _, inst = _only(session)
        inst.set_attr("0008,0090", "Real^Referrer")
        session.anonymize(report)
        assert inst.phi_status is PhiStatus.UNSCANNED
        ds, text = _export_and_report(session, tmp_path)
    assert 0x00120062 not in ds
    _review_with_condition_8(text, "patients 0, studies 0, instances 1")


def test_a_patient_edited_after_its_scan_stays_stale(tmp_path):
    _source(tmp_path)
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        report = session.audit()
        patient, _, _ = _only(session)
        patient.patient_name = "Other^Name"
        session.anonymize(report)
        assert patient.phi_status is PhiStatus.UNSCANNED
        assert _edited_since_its_status(patient)
        ds, text = _export_and_report(session, tmp_path)
    assert 0x00120062 not in ds
    _review_with_condition_8(text, "patients 1, studies 0, instances 0")


def test_a_never_scanned_instance_is_still_stamped_remediated(tmp_path):
    """The boundary: hand-built findings over an entity with no status
    (raw None) end REMEDIATED, as before."""
    _source(tmp_path)
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        _, _, inst = _only(session)
        assert inst._phi_status is None
        findings = list(session.audit())
        for entity in session._status_bearers():
            entity._phi_status = None
            entity._phi_status_policy = None
        session._scan_tally = None
        session.anonymize(findings)
        assert inst.phi_status is PhiStatus.REMEDIATED


def test_a_stale_entity_the_pass_did_not_touch_is_left_alone(tmp_path):
    """A stale study whose status the pass did not record keeps its raw
    status and revisions exactly."""
    _source(tmp_path, referrer="Orig^Ref")
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        report = session.audit()
        _, study, _ = _only(session)
        study.study_time = "120000"
        assert _edited_since_its_status(study)
        before = (study._phi_status, study._phi_status_policy,
                  study._phi_status_revision, study._revision)
        session.anonymize([f for f in report.findings
                           if f.entity_type == "Instance"])
        assert (study._phi_status, study._phi_status_policy,
                study._phi_status_revision, study._revision) == before


def test_a_re_audit_recovers_the_pass(tmp_path):
    _source(tmp_path)
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        session.audit()
        _, _, inst = _only(session)
        inst.set_attr("0008,0090", "Real^Referrer")
        session.anonymize(session.audit())
        assert inst.phi_status is PhiStatus.REMEDIATED
        ds, text = _export_and_report(session, tmp_path)
    assert ds[0x00120062].value == "YES"
    basis = _basis(text)
    assert len(basis) == 1 and "**Grade Basis:** PASS" in basis[0], basis


def test_an_edit_saved_across_a_reopen_stays_stale(tmp_path):
    _source(tmp_path)
    db = str(tmp_path / "s.db")
    with Session(db) as session:
        session.ingest(str(tmp_path / "in"))
        report = session.audit()
        _, _, inst = _only(session)
        sop = inst.sop_instance_uid
        inst.set_attr("0008,0090", "Real^Referrer")
        session.save(sync=True)
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT phi_status, phi_status_edited FROM instances "
            "WHERE sop_instance_uid=?", (sop,)).fetchall() == [
                ("unscanned", "identified")]
    with Session(db) as session:
        _, _, inst = _only(session)
        assert _edited_since_its_status(inst)
        session.anonymize(report)
        assert inst.phi_status is PhiStatus.UNSCANNED
        assert _edited_since_its_status(inst)
        session.save(sync=True)
        _, text = _export_and_report(session, tmp_path)
    _review_with_condition_8(text, "patients 0, studies 0, instances 1")
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT phi_status, phi_status_edited FROM instances").fetchall() == [
                ("unscanned", "identified")]


def _redactable(tmp_path):
    path = write_ct(tmp_path / "in" / "a.dcm", "PA", 7520, name="Alpha^Ann")
    ds = pydicom.dcmread(path)
    ds.DeviceSerialNumber = "B1-REDACT"
    ds.save_as(path)


def _redact(session):
    session.configuration.add_rule("B1-REDACT", redaction_zones=[[0, 4, 0, 4]])
    assert session.redact() == 1


def _settled(out, text, inst):
    assert inst.phi_status is PhiStatus.REMEDIATED
    assert out.get((0x0012, 0x0062)) is not None and out[0x0012, 0x0062].value == "YES"
    assert (0x0012, 0x0063) in out
    basis = _basis(text)
    assert len(basis) == 1 and basis[0].startswith("*   **Grade Basis:** PASS -- "), basis


def test_audit_then_redact_then_anonymize_over_that_report(tmp_path):
    """`audit()`, then `redact()`, then `anonymize(report)` (review of #896,
    finding 1; owner ruling, option a). Redaction carries the IDENTIFIED the
    audit left, because it writes no attribute the scan read, so the
    instance is current when the pass begins and the pass settles it: PASS,
    with the markers written, as on 7579d4df. Before the ruling, with #752
    and redaction carrying only REMEDIATED/CLEARED, this graded
    REVIEW_REQUIRED and withheld the markers.
    """
    _redactable(tmp_path)
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        report = session.audit()
        _patient, _study, inst = _only(session)
        assert inst.phi_status is PhiStatus.IDENTIFIED
        _redact(session)
        assert inst.phi_status is PhiStatus.IDENTIFIED
        session.anonymize(report)
        out, text = _export_and_report(session, tmp_path)
        _settled(out, text, inst)


def test_anonymize_then_redact_the_documented_order_settles(tmp_path):
    """The documented order (`docs/tutorials/redact-burned-in-pixels.md`):
    REMEDIATED is carried across the redaction, PASS, markers written."""
    _redactable(tmp_path)
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        session.anonymize(session.audit())
        _patient, _study, inst = _only(session)
        _redact(session)
        out, text = _export_and_report(session, tmp_path)
        _settled(out, text, inst)


def test_an_edit_before_the_redaction_still_holds_the_pass_stale(tmp_path):
    """An edit after the audit, then `redact()`, then `anonymize(report)`:
    redaction carries only a status current before it, so the edit is not
    hidden, and #752 keeps the instance stale through the pass. Condition 8,
    markers withheld."""
    _redactable(tmp_path)
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        report = session.audit()
        _patient, _study, inst = _only(session)
        inst.set_attr("0008,0090", "Real^Referrer")
        _redact(session)
        assert inst.phi_status is PhiStatus.UNSCANNED
        session.anonymize(report)
        out, text = _export_and_report(session, tmp_path)
        assert inst.phi_status is PhiStatus.UNSCANNED
        assert inst._phi_status is PhiStatus.IDENTIFIED
        assert (0x0012, 0x0062) not in out
        _review_with_condition_8(text, "patients 0, studies 0, instances 1")
